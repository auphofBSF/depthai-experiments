# coding=utf-8
from pathlib import Path

import cv2
import depthai
import numpy as np
from imutils.video import FPS

def to_planar(arr: np.ndarray, shape: tuple):
    return cv2.resize(arr, shape).transpose((2, 0, 1)).flatten()


def to_nn_result(nn_data):
    return np.array(nn_data.getFirstLayerFp16())


def to_tensor_result(packet):
    return {
        name: np.array(packet.getLayerFp16(name))
        for name in [tensor.name for tensor in packet.getRaw().tensors]
    }


def frame_norm(frame, *xy_vals):
    """
    nn data, being the bounding box locations, are in <0..1> range -
    they need to be normalized with frame width/height

    :param frame:
    :param xy_vals: the bounding box locations
    :return:
    """
    return (
        np.clip(np.array(xy_vals), 0, 1)
        * np.array(frame.shape[:2] * (len(xy_vals) // 2))[::-1]
    ).astype(int)


def run_nn(x_in, x_out, in_dict):
    nn_data = depthai.NNData()
    for key in in_dict:
        nn_data.setLayer(key, in_dict[key])
    x_in.send(nn_data)
    return x_out.tryGet()


class DepthAI:
    def __init__(self,file=None):
        print("Loading pipeline...")
        self.file = file
        self.fps_cam = FPS()
        self.fps_nn = FPS()
        self.create_pipeline()
        self.start_pipeline()
        self.fontScale = 1
        self.lineType = 0

    def create_pipeline(self):
        print("Creating pipeline...")
        self.pipeline = depthai.Pipeline()

        # cam = self.pipeline.createColorCamera()
        # cam.setPreviewSize(300, 300)
        # cam.setResolution(depthai.ColorCameraProperties.SensorResolution.THE_1080_P)
        # cam.setVideoSize(720,720)
        # cam.setInterleaved(False)
        # cam.setBoardSocket(depthai.CameraBoardSocket.RGB)
        # cam.setColorOrder(depthai.ColorCameraProperties.ColorOrder.BGR)
        # cam.initialControl.setManualFocus(130)
        # cam.setIspScale(2, 3)


        # vid_xout = self.pipeline.createXLinkOut()
        # vid_xout.setStreamName("video")
        # cam.video.link(vid_xout.input)

        left = self.pipeline.createMonoCamera()
        left.setResolution(depthai.MonoCameraProperties.SensorResolution.THE_720_P)
        left.setBoardSocket(depthai.CameraBoardSocket.LEFT)

        right = self.pipeline.createMonoCamera()
        right.setResolution(depthai.MonoCameraProperties.SensorResolution.THE_720_P)
        right.setBoardSocket(depthai.CameraBoardSocket.RIGHT)

        # Create a node that will produce the depth map (using disparity output as it's easier to visualize depth this way)
        stereo = self.pipeline.createStereoDepth()
        stereo.setConfidenceThreshold(245)
        # stereo.setLeftRightCheck(True)
        left.out.link(stereo.left)
        right.out.link(stereo.right)

        # For palm detection NN
        self.manip = self.pipeline.createImageManip()
        self.manip.initialConfig.setResize(128, 128)
        self.manip.initialConfig.setFrameType(depthai.ImgFrame.Type.BGR888p)
        stereo.rectifiedRight.link(self.manip.inputImage)

        mobilenet_manip = self.pipeline.createImageManip()
        mobilenet_manip.initialConfig.setResize(300, 300)
        mobilenet_manip.initialConfig.setFrameType(depthai.ImgFrame.Type.BGR888p)
        stereo.rectifiedRight.link(mobilenet_manip.inputImage)

        vid_xout = self.pipeline.createXLinkOut()
        vid_xout.setStreamName("video")
        stereo.rectifiedRight.link(vid_xout.input)

        mobilenet_nn = self.pipeline.createMobileNetSpatialDetectionNetwork()
        mobilenet_nn.setConfidenceThreshold(0.5)
        mobilenet_nn.input.setBlocking(False)
        mobilenet_nn.setBoundingBoxScaleFactor(0.3)
        mobilenet_nn.setDepthLowerThreshold(200)
        mobilenet_nn.setDepthUpperThreshold(3000)
        mobilenet_nn.setBlobPath(str(Path("models/mobilenet-ssd_openvino_2021.2_6shave.blob").resolve().absolute()))
        mobilenet_nn.setNumInferenceThreads(2)

        mobilenet_manip.out.link(mobilenet_nn.input)
        stereo.depth.link(mobilenet_nn.inputDepth)

        detOut = self.pipeline.createXLinkOut()
        detOut.setStreamName("det")
        mobilenet_nn.out.link(detOut.input)

        depth_out = self.pipeline.createXLinkOut()
        depth_out.setStreamName("depth")
        mobilenet_nn.passthroughDepth.link(depth_out.input)

        self.create_nns()

        print("Pipeline created.")

    def create_nns(self):
        pass

    def create_nn(self, model_path: str, model_name: str, first: bool = False):
        """
        :param model_path: model path
        :param model_name: model abbreviation
        :param first: Is it the first model
        :return:
        """
        # NeuralNetwork
        print(f"Creating {model_path} Neural Network...")
        model_nn = self.pipeline.createNeuralNetwork()
        model_nn.setBlobPath(str(Path(f"{model_path}").resolve().absolute()))
        model_nn.input.setBlocking(False)
        if first:
            print("linked manip.out to model_nn.input")
            self.manip.out.link(model_nn.input)

        model_nn_xout = self.pipeline.createXLinkOut()
        model_nn_xout.setStreamName(f"{model_name}_nn")
        model_nn.out.link(model_nn_xout.input)

    def start_pipeline(self):
        self.device = depthai.Device(self.pipeline)
        print("Starting pipeline...")
        self.device.startPipeline()

        self.start_nns()

        self.vidQ = self.device.getOutputQueue(name="video", maxSize=4, blocking=False)

        self.detQ = self.device.getOutputQueue(name="det", maxSize=4, blocking=False)
        self.depthQ = self.device.getOutputQueue(name="depth", maxSize=4, blocking=False)

    def start_nns(self):
        pass

    def put_text(self, text, dot, color=(0, 0, 255), font_scale=None, line_type=None):
        font_scale = font_scale if font_scale else self.fontScale
        line_type = line_type if line_type else self.lineType
        dot = tuple(dot[:2])
        cv2.putText(
            img=self.debug_frame,
            text=text,
            org=dot,
            fontFace=cv2.FONT_HERSHEY_COMPLEX,
            fontScale=font_scale,
            color=color,
            lineType=line_type,
        )

    def draw_bbox(self, bbox, color):
        cv2.rectangle(
            img=self.debug_frame,
            pt1=(bbox[0], bbox[1]),
            pt2=(bbox[2], bbox[3]),
            color=color,
            thickness=2,
        )
        cv2.rectangle(
            img=self.depthFrameColor,
            pt1=(bbox[0], bbox[1]),
            pt2=(bbox[2], bbox[3]),
            color=color,
            thickness=2,
        )

    def drawDetections(self, frame):
        # MobilenetSSD label texts
        labelMap = ["background", "aeroplane", "bicycle", "bird", "boat", "bottle", "bus", "car", "cat", "chair", "cow",
                    "diningtable", "dog", "horse", "motorbike", "person", "pottedplant", "sheep", "sofa", "train", "tvmonitor"]
        height = frame.shape[0]
        width  = frame.shape[1]
        color = (250,0,0)
        for detection in self.detections:
            # Denormalize bounding box
            x1 = int(detection.xmin * width)
            x2 = int(detection.xmax * width)
            y1 = int(detection.ymin * height)
            y2 = int(detection.ymax * height)
            try:
                label = labelMap[detection.label]
            except:
                label = detection.label
            cv2.putText(frame, str(label), (x1 + 10, y1 + 20), cv2.FONT_HERSHEY_TRIPLEX, 0.5, color)
            cv2.putText(frame, "{:.2f}".format(detection.confidence*100), (x1 + 10, y1 + 35), cv2.FONT_HERSHEY_TRIPLEX, 0.5, color)
            cv2.putText(frame, f"X: {int(detection.spatialCoordinates.x)} mm", (x1 + 10, y1 + 50), cv2.FONT_HERSHEY_TRIPLEX, 0.5, color)
            cv2.putText(frame, f"Y: {int(detection.spatialCoordinates.y)} mm", (x1 + 10, y1 + 65), cv2.FONT_HERSHEY_TRIPLEX, 0.5, color)
            cv2.putText(frame, f"Z: {int(detection.spatialCoordinates.z)} mm", (x1 + 10, y1 + 80), cv2.FONT_HERSHEY_TRIPLEX, 0.5, color)

            cv2.rectangle(frame, (x1, y1), (x2, y2), color, cv2.FONT_HERSHEY_SIMPLEX)

    def parse(self):
        self.debug_frame = self.frame.copy()

        self.parse_fun()

        self.drawDetections(self.debug_frame)

        cv2.imshow("Camera_view", self.debug_frame)

        if self.depthFrameColor is not None:
            self.drawDetections(self.depthFrameColor)
            cv2.imshow("depth", self.depthFrameColor)

        self.fps_cam.update()
        if cv2.waitKey(1) == ord("q"):
            cv2.destroyAllWindows()
            self.fps_cam.stop()
            self.fps_nn.stop()
            print(f"FPS_CAMERA: {self.fps_cam.fps():.2f} , FPS_NN: {self.fps_nn.fps():.2f}")
            raise StopIteration()


    def averageDepth(self, roi):
        if self.depth is None: return 0
        bbox = np.array(roi)
        offsetX = int((bbox[2] - bbox[0]) / 2.5)
        offsetY = int((bbox[3] - bbox[1]) / 2.5)
        bbox[0] += offsetX
        bbox[1] += offsetY
        bbox[2] -= offsetX
        bbox[3] -= offsetY
        cv2.imshow("rgbCropped", self.debug_frame[bbox[1]:bbox[3], bbox[0]:bbox[2]])
        depthRoi = self.depth[bbox[1]:bbox[3], bbox[0]:bbox[2]]
        depthRoi[depthRoi < 200] = 200
        depthRoi[depthRoi > 2000] = 2000
        print(np.average(depthRoi))
        cv2.rectangle(self.depthFrameColor, (bbox[0], bbox[1]), (bbox[2], bbox[3]), (100, 0, 100), 4)
        cv2.rectangle(self.debug_frame, (bbox[0], bbox[1]), (bbox[2], bbox[3]), (100, 0, 100), 4)


    def parse_fun(self):
        pass

    def run_camera(self):
        baseline = 75 #mm
        disp_levels = 91
        focal = 857.0

        self.depthFrameColor = None
        self.detections = []
        self.depth = None

        while True:
            in_rgb = self.vidQ.tryGet()
            if in_rgb is not None:
                self.frame = in_rgb.getCvFrame()
                self.frame = self.frame[0:720, 280:1000]
                try:
                    self.parse()
                except StopIteration:
                    break

            in_det = self.detQ.tryGet()
            if in_det is not None:
                self.detections = in_det.detections

            in_disp = self.depthQ.tryGet()
            if in_disp is not None:
                disp = in_disp.getFrame()
                # Crop image to be 1:1 as RGB frame
                disp = disp[0:720, 280:1000]
                with np.errstate(divide='ignore'):
                    self.depth = (disp_levels * baseline * focal / disp).astype(np.uint16)
                depthFrameColor = cv2.normalize(self.depth, None, 255, 0, cv2.NORM_INF, cv2.CV_8UC1)
                depthFrameColor = cv2.equalizeHist(depthFrameColor)
                self.depthFrameColor = cv2.applyColorMap(depthFrameColor, cv2.COLORMAP_JET)
            # in_det = self.detQ.tryGet()

    def run(self):
        self.fps_cam.start()
        self.fps_nn.start()
        self.run_camera()
        del self.device


def distance(pt1, pt2):
    return np.sqrt(np.float_power(np.array(pt1) - pt2, 2).sum())

def point_mapping(dot, center, original_side_length, target_side_length):
    """

    :param dot: point coordinates
    :param center: frame center point coordinates
    :param original_side_length: source side length
    :param target_side_length: target side length
    :return:
    """
    if isinstance(original_side_length, (int, float)):
        original_side_length = np.array((original_side_length, original_side_length))
    if isinstance(target_side_length, (int, float)):
        target_side_length = np.array((target_side_length, target_side_length))

    return center + (np.array(dot) - center) * (
        np.array(target_side_length) / original_side_length
    )

def sigmoid(x):
    return (1.0 + np.tanh(0.5 * x)) * 0.5

def decode_boxes(raw_boxes, anchors, shape, num_keypoints):
    """
    Converts the predictions into actual coordinates using the anchor boxes.
    Processes the entire batch at once.
    """
    boxes = np.zeros_like(raw_boxes)
    x_scale, y_scale = shape

    x_center = raw_boxes[..., 0] / x_scale * anchors[:, 2] + anchors[:, 0]
    y_center = raw_boxes[..., 1] / y_scale * anchors[:, 3] + anchors[:, 1]

    w = raw_boxes[..., 2] / x_scale * anchors[:, 2]
    h = raw_boxes[..., 3] / y_scale * anchors[:, 3]

    boxes[..., 1] = y_center - h / 2.0  # xmin
    boxes[..., 0] = x_center - w / 2.0  # ymin
    boxes[..., 3] = y_center + h / 2.0  # xmax
    boxes[..., 2] = x_center + w / 2.0  # ymax

    for k in range(num_keypoints):
        offset = 4 + k * 2
        keypoint_x = raw_boxes[..., offset] / x_scale * anchors[:, 2] + anchors[:, 0]
        keypoint_y = (
            raw_boxes[..., offset + 1] / y_scale * anchors[:, 3] + anchors[:, 1]
        )
        boxes[..., offset] = keypoint_x
        boxes[..., offset + 1] = keypoint_y

    return boxes

def raw_to_detections(raw_box_tensor, raw_score_tensor, anchors_, shape, num_keypoints):
    """

    This function converts these two "raw" tensors into proper detections.
    Returns a list of (num_detections, 17) tensors, one for each image in
    the batch.

    This is based on the source code from:
    mediapipe/calculators/tflite/tflite_tensors_to_detections_calculator.cc
    mediapipe/calculators/tflite/tflite_tensors_to_detections_calculator.proto
    """
    detection_boxes = decode_boxes(raw_box_tensor, anchors_, shape, num_keypoints)
    detection_scores = sigmoid(raw_score_tensor).squeeze(-1)
    output_detections = []
    for i in range(raw_box_tensor.shape[0]):
        boxes = detection_boxes[i]
        scores = np.expand_dims(detection_scores[i], -1)
        output_detections.append(np.concatenate((boxes, scores), -1))
    return output_detections

def non_max_suppression(boxes, probs=None, angles=None, overlapThresh=0.3):
    if len(boxes) == 0:
        return [], []

    if boxes.dtype.kind == "i":
        boxes = boxes.astype("float")

    pick = []

    x1 = boxes[:, 0]
    y1 = boxes[:, 1]
    x2 = boxes[:, 2]
    y2 = boxes[:, 3]

    area = (x2 - x1 + 1) * (y2 - y1 + 1)
    idxs = y2

    if probs is not None:
        idxs = probs

    idxs = np.argsort(idxs)

    while len(idxs) > 0:
        last = len(idxs) - 1
        i = idxs[last]
        pick.append(i)

        xx1 = np.maximum(x1[i], x1[idxs[:last]])
        yy1 = np.maximum(y1[i], y1[idxs[:last]])
        xx2 = np.minimum(x2[i], x2[idxs[:last]])
        yy2 = np.minimum(y2[i], y2[idxs[:last]])

        w = np.maximum(0, xx2 - xx1 + 1)
        h = np.maximum(0, yy2 - yy1 + 1)

        overlap = (w * h) / area[idxs[:last]]

        idxs = np.delete(
            idxs, np.concatenate(([last], np.where(overlap > overlapThresh)[0]))
        )

    if angles is not None:
        return boxes[pick].astype("int"), angles[pick]
    return boxes[pick].astype("int")


class Main(DepthAI):
    def __init__(self, file=None):
        super().__init__(file)

    def create_nns(self):
        self.create_nn("models/palm_detection_openvino_2021.3_6shave.blob", "palm", first=True)

    def start_nns(self):
        self.palm_nn = self.device.getOutputQueue("palm_nn", maxSize=4, blocking=False)

    def run_palm(self):
        """
        Each palm detection is a tensor consisting of 19 numbers:
            - ymin, xmin, ymax, xmax
            - x,y-coordinates for the 7 key_points
            - confidence score
        :return:
        """
        shape = (128, 128)
        num_keypoints = 7
        min_score_thresh = 0.7
        anchors = np.load("anchors_palm.npy")

        nn_data = self.palm_nn.tryGet()

        if nn_data is None:
            return

        # Run the neural network
        results = to_tensor_result(nn_data)

        raw_box_tensor = results.get("regressors").reshape(-1, 896, 18)  # regress
        raw_score_tensor = results.get("classificators").reshape(-1, 896, 1)  # classification

        detections = raw_to_detections(
            raw_box_tensor, raw_score_tensor, anchors, shape, num_keypoints
        )
        # print(detections.shape)

        self.palm_coords = [
            frame_norm(self.frame, *obj[:4])
            for det in detections
            for obj in det
            if obj[-1] > min_score_thresh
        ]

        self.palm_confs = [
            obj[-1] for det in detections for obj in det if obj[-1] > min_score_thresh
        ]

        if len(self.palm_coords) == 0:
            return

        self.palm_coords = non_max_suppression(
            boxes=np.concatenate(self.palm_coords).reshape(-1, 4),
            probs=self.palm_confs,
            overlapThresh=0.1,
        )

        for bbox in self.palm_coords:
            self.draw_bbox(bbox, (10, 245, 10))

    # @timer
    def parse_fun(self):
        self.run_palm()


if __name__ == "__main__":
    Main().run()
