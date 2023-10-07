"""This module contains :class:`.TransformNode`, a :term:`ROS` node generating the
:term:`query` and :term:`reference` image pair by rotating the reference
image based on :term:`vehicle` heading, and then cropping it based on the
:term:`camera` information.
"""
from dataclasses import dataclass
from typing import Final, Optional, Tuple, Union, get_args

import cv2
import numpy as np
from ament_index_python.packages import get_package_share_directory
from cv_bridge import CvBridge
from geographic_msgs.msg import GeoPoint, GeoPose, GeoPoseStamped
from geometry_msgs.msg import Quaternion
from mavros_msgs.msg import Altitude
from rcl_interfaces.msg import ParameterDescriptor
from rclpy.node import Node
from rclpy.qos import QoSPresetProfiles
from sensor_msgs.msg import CameraInfo, Image

from .. import messaging
from .._assertions import assert_type
from .._decorators import ROS, narrow_types
from ..static_configuration import (
    BBOX_NODE_NAME,
    GIS_NODE_NAME,
    ROS_NAMESPACE,
    ROS_TOPIC_RELATIVE_ORTHOIMAGE,
    ROS_TOPIC_RELATIVE_PNP_IMAGE,
)


class TransformNode(Node):
    """Publishes :term:`query` and :term:`reference` image pair

    Rotates the reference image based on :term:`vehicle` heading, and then
    crops it based on :term:`camera` image resolution.
    """

    @dataclass(frozen=True)
    class _PoseEstimationIntermediateOutputs:
        """Data generated by pre-processing that is useful for post-processing

        :ivar affine_transform: Transformation from orthoimage to rotated &
            cropped (aligned with video image) frame
        :ivar camera_yaw_degrees: Camera yaw in degrees in NED frame
        """

        affine_transform: np.ndarray
        camera_yaw_degrees: float

    @dataclass(frozen=True)
    class _PoseEstimationContext:
        """
        Required context for post-processing an estimated
        :class:`geometry_msgs.msg.Pose` into a :class:`geographic_msgs.msg.GeoPose`
        that should be frozen at same time as inputs.

        :ivar orthoimage: Orthoimage used for matching. In post-processing
            this is required to get a geotransformation matrix from the
            orthoimage pixel frame to WGS 84 coordinates. It is lazily
            evaluated in post-processing even though it could already be
            computed earlier - all required information is contained in the
            orthoimage itself.
        :ivar camera_geopose: :term:`Camera` :term:`geopose` with orientation
            in :term:`ENU` frame. The pose estimation is done against a
            rotated orthoimage and this is needed to get the pose in the
            original coordinate frame.
        """

        orthoimage: Image
        camera_geopose: GeoPoseStamped

    ROS_D_POSE_ESTIMATOR_ENDPOINT = "http://localhost:8090/predictions/loftr"
    """Default pose estimator endpoint URL"""

    ROS_D_MISC_MAX_PITCH = 30
    """Default maximum camera pitch from nadir in degrees for attempting to
    estimate pose against reference map

    .. seealso::
        :py:attr:`.ROS_D_MAP_UPDATE_MAX_PITCH`
        :py:attr:`.ROS_D_MAP_UPDATE_GIMBAL_PROJECTION`
    """

    ROS_D_MISC_MIN_MATCH_ALTITUDE = 80
    """Default minimum ground altitude in meters under which matches against
    map will not be attempted"""

    ROS_D_MISC_ATTITUDE_DEVIATION_THRESHOLD = 10
    """Magnitude of allowed attitude deviation of estimate from expectation in
    degrees"""

    _ROS_PARAM_DESCRIPTOR_READ_ONLY: Final = ParameterDescriptor(read_only=True)
    """A read only ROS parameter descriptor"""

    def __init__(self, *args, **kwargs) -> None:
        """Class initializer

        :param args: Positional arguments to parent :class:`.Node` constructor
        :param kwargs: Keyword arguments to parent :class:`.Node` constructor
        """
        super().__init__(*args, **kwargs)

        self._package_share_dir = get_package_share_directory("gisnav")

        # Converts image_raw to cv2 compatible image
        self._cv_bridge = CvBridge()

        # Calling these decorated properties the first time will setup
        # subscriptions to the appropriate ROS topics
        self.orthoimage
        self.camera_geopose
        self.camera_info
        self.image

    @property
    @ROS.parameter(ROS_D_MISC_MAX_PITCH)
    def max_pitch(self) -> Optional[int]:
        """Max :term:`camera` pitch in degrees from :term:`nadir` beyond which
        :term:`pose` estimation will not be attempted
        """

    @property
    @ROS.parameter(ROS_D_MISC_MIN_MATCH_ALTITUDE)
    def min_match_altitude(self) -> Optional[int]:
        """Minimum :term:`vehicle` :term:`altitude` in meters :term:`AGL` below which
        :term:`pose` estimation will not be attempted
        """

    @property
    @ROS.subscribe(
        f"/{ROS_NAMESPACE}"
        f'/{ROS_TOPIC_RELATIVE_ORTHOIMAGE.replace("~", GIS_NODE_NAME)}',
        QoSPresetProfiles.SENSOR_DATA.value,
    )
    def orthoimage(self) -> Optional[Image]:
        """Subscribed :term:`orthoimage` for :term:`pose` estimation"""

    # TODO need some way of not sending stuff to pnp if looks like camera is looking in the wrong direction
    @property
    # @ROS.max_delay_ms(messaging.DELAY_DEFAULT_MS)
    @ROS.subscribe(
        f"/{ROS_NAMESPACE}"
        f'/{ROS_TOPIC_RELATIVE_CAMERA_GEOPOSE.replace("~", BBOX_NODE_NAME)}',
        QoSPresetProfiles.SENSOR_DATA.value,
    )
    def camera_geopose(self) -> Optional[GeoPoseStamped]:
        """:term:`Camera` :term:`geopose`, or None if not available"""

    @property
    # @ROS.max_delay_ms(messaging.DELAY_SLOW_MS) - gst plugin does not enable timestamp?
    @ROS.subscribe(messaging.ROS_TOPIC_CAMERA_INFO, QoSPresetProfiles.SENSOR_DATA.value)
    def camera_info(self) -> Optional[CameraInfo]:
        """Camera info for determining appropriate :attr:`.orthoimage` resolution"""

    @property
    # @ROS.max_delay_ms(messaging.DELAY_FAST_MS) - gst plugin does not enable timestamp?
    @ROS.subscribe(
        messaging.ROS_TOPIC_IMAGE,
        QoSPresetProfiles.SENSOR_DATA.value,
    )
    def image(self) -> Optional[Image]:
        """Raw image data from vehicle camera for pose estimation"""

    def _should_estimate_geopose(self) -> bool:
        """Determines whether :attr:`.vehicle_estimated_geopose` should be called

        Match should be attempted if (1) a reference map has been retrieved,
        (2) camera roll or pitch is not too high (e.g. facing horizon instead
        of nadir), and (3) drone is not flying too low.

        :return: True if pose estimation be attempted
        """

        @narrow_types(self)
        def _should_estimate(altitude: Altitude, max_pitch: int, min_alt: int):
            # Check condition (2) - whether camera roll/pitch is too large
            if self._camera_roll_or_pitch_too_high(max_pitch):
                self.get_logger().warn(
                    f"Camera roll or pitch not available or above limit {max_pitch}. "
                    f"Skipping pose estimation."
                )
                return False

            # Check condition (3) - whether vehicle altitude is too low
            assert min_alt > 0
            if altitude.terrain is np.nan:
                self.get_logger().warn(
                    "Cannot determine altitude AGL, skipping map update."
                )
                return False
            if altitude.terrain < min_alt:
                self.get_logger().warn(
                    f"Assumed altitude {altitude.terrain} was lower "
                    f"than minimum threshold for matching ({min_alt}) or could not "
                    f"be determined. Skipping pose estimation."
                )
                return False

            return True

        return bool(
            _should_estimate(self.altitude, self.max_pitch, self.min_match_altitude)
        )

    @staticmethod
    def _extract_yaw(q: Quaternion) -> float:
        """Calculate the yaw angle from a quaternion in the ENU frame.

        Returns yaw with origin centered at North (i.e. applies a 90 degree adjustment).

        :param q: A list containing the quaternion [qx, qy, qz, qw].
        :return: The yaw angle in degrees.
        """
        enu_yaw = np.arctan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y**2 + q.z**2))
        enu_yaw_deg = np.degrees(enu_yaw)

        # Convert ENU yaw to heading with North as origin
        heading = 90.0 - enu_yaw_deg

        # Normalize to [0, 360) range
        heading = (heading + 360) % 360

        return heading

    @staticmethod
    def _determine_utm_zone(longitude):
        """Determine the UTM zone for a given longitude."""
        return int((longitude + 180) / 6) + 1

    @property
    @ROS.publish(
        ROS_TOPIC_RELATIVE_PNP_IMAGE,
        QoSPresetProfiles.SENSOR_DATA.value,
    )
    def pnp_image(self) -> Optional[Image]:
        """Published :term:`stacked <stack>` image consisting of query image,
        reference image, and reference elevation raster (:term:`DEM`).

        .. note::
            Semantically not a single image, but a stack of two 8-bit grayscale
            images and one 16-bit "image-like" elevation reference, stored in a
            compact way in an existing message type so to avoid having to also
            publish custom :term:`ROS` message definitions.
        """

        @narrow_types(self)
        def _pnp_image(
            image: Image,
            orthoimage: Image,
            context: _PoseEstimationContext,
        ) -> Optional[Image]:
            """Rotate and crop and orthoimage stack to align with query image"""

            query_img = self._cv_bridge.imgmsg_to_cv2(image, desired_encoding="mono8")

            orthoimage_stack = self._cv_bridge.imgmsg_to_cv2(
                self.orthoimage, desired_encoding="passthrough"
            )

            assert orthoimage_stack.shape[2] == 3, (
                f"Orthoimage stack channel count was {orthoimage_stack.shape[2]} "
                f"when 3 was expected (one channel for 8-bit grayscale reference "
                f"image and two 8-bit channels for 16-bit elevation reference)"
            )

            # Rotate and crop orthoimage stack
            camera_yaw_degrees = self._extract_yaw(
                context.camera_geopose.pose.orientation
            )
            crop_shape: Tuple[int, int] = query_img.shape[0:2]
            # orthoimage_stack = np.dstack((reference_img, elevation_reference))
            orthoimage_stack, affine_transform = self._rotate_and_crop_image(
                orthoimage_stack, camera_yaw_degrees, crop_shape
            )

            # Add query image on top to complete full image stack
            pnp_image_stack = np.dstack((query_array, orthoimage_stack))

            pnp_image_msg = self._cv_bridge.cv2_to_imgmsg(
                pnp_image_stack, encoding="passthrough"
            )
            pnp_image_msg.header.stamp = image.header.stamp

            # Figure out local frame - compute proj string
            # UPDATE: geotransform is now GISNode.orthoimage frame_id proj string
            # geotransform = self._get_geotransformation_matrix(context.orthoimage)
            r, t = pose
            t_world = np.matmul(r.T, -t)
            t_world_homogenous = np.vstack((t_world, [1]))
            try:
                t_unrotated_uncropped = (
                    np.linalg.inv(affine_transform) @ t_world_homogenous  # t
                )
            except np.linalg.LinAlgError as _:  # noqa: F841
                self.get_logger().warn(
                    "Rotation and cropping was non-invertible, cannot compute "
                    "GeoPoint and Altitude"
                )
                return None

            # ESD (cv2 x is width) to SEU (numpy array y is south) (x y might
            # be flipped because cv2)
            t_unrotated_uncropped = np.array(
                (
                    t_unrotated_uncropped[1],
                    t_unrotated_uncropped[0],
                    -t_unrotated_uncropped[2],
                    t_unrotated_uncropped[3],
                )
            )
            t_wgs84 = geotransform @ t_unrotated_uncropped
            lat, lon = t_wgs84.squeeze()[1::-1]
            float(t_wgs84[2])

            if geopoint is not None and altitude is not None:
                r, t = pose
                # r = messaging.quaternion_to_rotation_matrix(pose.orientation)

                # Rotation matrix is assumed to be in cv2.solvePnPRansac world
                # coordinate system (SEU axes), need to convert to NED axes after
                # reverting rotation and cropping
                try:
                    r = (
                        self._seu_to_ned_matrix
                        @ np.linalg.inv(intermediate_outputs.affine_transform[:3, :3])
                        @ r.T  # @ -t
                    )
                except np.linalg.LinAlgError as _:  # noqa: F841
                    self.get_logger().warn(
                        "Cropping and rotation was non-invertible, canot estimate "
                        "GeoPoint and Altitude."
                    )
                    return None

                messaging.rotation_matrix_to_quaternion(r)

            utm_zone = self._determine_utm_zone(
                context.camera_geopose.pose.position.longitude
            )
            proj_string = self._to_proj_string(r, t, utm_zone)
            pnp_image_msg.header.frame_id = proj_string

            return pnp_image_msg

        return _pnp_image(
            self.image,
            self.orthoimage,
            self._pose_estimation_context,
        )

    @narrow_types
    def _post_process_pose(
        self,
        inputs,
        pose: Tuple[np.ndarray, np.ndarray],
        intermediate_outputs: _PoseEstimationIntermediateOutputs,
        context: _PoseEstimationContext,
    ) -> Optional[Tuple[GeoPoint, Altitude, Quaternion]]:
        """
        Post process estimated pose to vehicle GeoPoint, Altitude and gimbal
        Quaternion estimates

        Estimates camera GeoPoint (WGS84 coordinates + altitude in meters
        above mean sea level (AMSL) and ground level (AGL).
        """

        altitude = Altitude(
            header=context.ground_track_elevation.header,
            monotonic=0.0,  # TODO
            amsl=alt + context.ground_track_elevation.amsl,
            local=0.0,  # TODO
            relative=0.0,  # TODO
            terrain=alt,
            bottom_clearance=alt,
        )
        geopoint = GeoPoint(
            altitude=alt + context.ground_track_geopose.pose.position.altitude,
            latitude=lat,
            longitude=lon,
        )

        if geopoint is not None and altitude is not None:
            r, t = pose
            # r = messaging.quaternion_to_rotation_matrix(pose.orientation)

            # Rotation matrix is assumed to be in cv2.solvePnPRansac world
            # coordinate system (SEU axes), need to convert to NED axes after
            # reverting rotation and cropping
            try:
                r = (
                    self._seu_to_ned_matrix
                    @ np.linalg.inv(intermediate_outputs.affine_transform[:3, :3])
                    @ r.T  # @ -t
                )
            except np.linalg.LinAlgError as _:  # noqa: F841
                self.get_logger().warn(
                    "Cropping and rotation was non-invertible, canot estimate "
                    "GeoPoint and Altitude."
                )
                return None

            quaternion = messaging.rotation_matrix_to_quaternion(r)
            return geopoint, altitude, quaternion
        else:
            return None

    # @property
    # def _esu_to_ned_matrix(self):
    #    """Transforms from ESU to NED axes"""
    #    transformation_matrix = np.array(
    #        [[0, 1, 0], [1, 0, 0], [0, 0, -1]]  # E->N  # S->E  # U->D
    #    )
    #    return transformation_matrix

    @property
    def _seu_to_ned_matrix(self):
        """Transforms from ESU to NED axes"""
        transformation_matrix = np.array(
            [[-1, 0, 0], [0, 1, 0], [0, 0, -1]]  # S->N  # E->E  # U->D
        )
        return transformation_matrix

    @property
    def _pose_estimation_context(self) -> Optional[_PoseEstimationContext]:
        """Gather all required inputs for pose estimation into one context
        in order to avoid using the same message with multiple different
        timestamps when computing the pose estimate
        """

        @narrow_types(self)
        def _pose_estimation_context(
            orthoimage: Image,
            camera_geopose: GeoPoseStamped,
        ):
            return self._PoseEstimationContext(
                orthoimage=orthoimage, camera_geopose=camera_geopose
            )

        return _pose_estimation_context(self.orthoimage, self.camera_geopose)

    @staticmethod
    def _get_rotation_matrix(image: np.ndarray, degrees: float) -> np.ndarray:
        height, width = image.shape[:2]
        cx, cy = height // 2, width // 2
        r = cv2.getRotationMatrix2D((cx, cy), degrees, 1.0)
        return r

    @staticmethod
    def _get_translation_matrix(dx, dy):
        t = np.float32([[1, 0, dx], [0, 1, dy]])
        return t

    @classmethod
    def _get_affine_matrix(
        cls, image: np.ndarray, degrees: float, crop_height: int, crop_width: int
    ) -> np.ndarray:
        """Creates affine transformation that rotates around center and then
        center-crops an image.

        .. note::
            Returns matrix in 3D since this matrix will not only be used for rotating
            and cropping the orthoimage rasters but also for converting 3D pose
            estimates in the rotated and cropped orthoimage frame back to the original
            unrotated and uncropped frame (from where it will then be converted to
            geocoordinates).

        Returns affine matrix padded to 3D (4x4 matrix) in the following format:
            [ R11  R12  0   Tx ]
            [ R21  R22  0   Ty ]
            [ 0    0    1   0  ]
            [ 0    0    0   1  ]
        where R11, R12, R21, R22 represents the rotation matrix, and Tx, Ty represent
        the translation along the x and y axis.

        :return: The affine transformation matrix in homogenous format as masked
            numpy array. Masking for use in 2D operations (e.g. cv2.warpAffine).
        """
        r = cls._get_rotation_matrix(image, degrees)
        assert r.shape == (2, 3)
        dx = (image.shape[0] - crop_height) // 2
        dy = (image.shape[1] - crop_width) // 2
        t = cls._get_translation_matrix(dx, dy)
        assert t.shape == (2, 3)

        # Combine rotation and translation to get the final affine transformation
        affine_2d = np.dot(t, np.vstack([r, [0, 0, 1]]))

        # Convert 2D affine matrix to 3D affine matrix
        # Create a 4x4 identity matrix
        affine_3d = np.eye(4)

        # Insert the 2D affine transformation into the 3D matrix
        affine_3d[:2, :2] = affine_2d[:, :2]
        affine_3d[:2, 3] = affine_2d[:, 2]

        assert affine_3d.shape == (4, 4)

        # Translation hack to make cv2.warpAffine warp the image into the top left
        # corner so that the output size argument of cv2.warpAffine acts as a
        # center-crop
        t[:2, 2] = -t[:2, 2][::-1]
        affine_hack = np.dot(t, np.vstack([r, [0, 0, 1]]))

        affine_3d[:2, :2] = affine_hack[:2, :2]
        affine_3d[:2, 3] = affine_hack[:2, 2]
        return affine_3d

    @classmethod
    def _rotate_and_crop_image(
        cls, image: np.ndarray, degrees: float, shape: Tuple[int, int]
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        Rotates around center and then center-crops image

        Cached because the same rotated image is expected to be used for multiple
        matches.

        :return: Tuple of rotated and cropped image, and used affine
            transformation matrix
        """
        # Image can have any number of channels
        affine = cls._get_affine_matrix(image, degrees, *shape)
        affine_2d = np.delete(affine, 2, 1)
        affine_2d = affine_2d[:2, :]
        return cv2.warpAffine(image, affine_2d, shape[::-1]), affine

    def _off_nadir_pitch(self, q: Quaternion) -> float:
        """Returns :term:`off-nadir <nadir>`pitch angle in degrees of input
        :term:`ENU` quaternion

        :param q: Quaternion in ENU frame
        :return: Off-nadir angle in degrees of the input quaternion
        """
        pitch_angle = np.arcsin(2 * (q.w * q.y - q.x * q.z))
        off_nadir_angle = np.pi / 2 - pitch_angle  # in radians
        return np.degrees(off_nadir_angle)

    def _camera_roll_or_pitch_too_high(self, max_pitch: Union[int, float]) -> bool:
        """Returns True if (set) camera roll or pitch exceeds given limit OR
        camera pitch is unknown

        Used to determine whether camera roll or pitch is too high up from
        nadir to make matching against a map worthwhile.

        :param max_pitch: The limit for the pitch in degrees from nadir over
            which it will be considered too high
        :return: True if pitch is too high
        """
        assert_type(max_pitch, get_args(Union[int, float]))
        if self.camera_geopose is not None:
            off_nadir_pitch_deg = self._off_nadir_pitch(
                self.camera_geopose.pose.orientation
            )

            if off_nadir_pitch_deg > max_pitch:
                self.get_logger().warn(
                    f"Camera pitch is {off_nadir_pitch_deg} degrees off nadir and "
                    f"above limit {max_pitch}."
                )
                return True
            else:
                self.get_logger().debug(
                    f"Camera pitch is {off_nadir_pitch_deg} degrees off nadir"
                )
                return False
        else:
            self.get_logger().warn(
                "Gimbal attitude was not available, assuming camera pitch too high."
            )
            return True
