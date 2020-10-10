import os
# comment out below line to enable tensorflow logging outputs
#os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
import time
import tensorflow as tf
physical_devices = tf.config.experimental.list_physical_devices('GPU')
if len(physical_devices) > 0:
    tf.config.experimental.set_memory_growth(physical_devices[0], True)
from absl import app, flags, logging
from absl.flags import FLAGS
import core.utils as utils
from core.yolov4 import filter_boxes
from tensorflow.python.saved_model import tag_constants
from core.config import cfg
from PIL import Image
import cv2
import numpy as np
import matplotlib
matplotlib.use('agg')
import matplotlib.pyplot as plt
from tensorflow.compat.v1 import ConfigProto
from tensorflow.compat.v1 import InteractiveSession

# deep sort imports
from deep_sort import preprocessing, nn_matching
from deep_sort.detection import Detection
from deep_sort.tracker import Tracker
from tools import generate_detections as gdet

# flags
flags.DEFINE_string('framework', 'tf', '(tf, tflite, trt')
flags.DEFINE_string('weights', './checkpoints/yolov4-416',
                    'path to weights file')
flags.DEFINE_integer('size', 416, 'resize images to')
flags.DEFINE_boolean('tiny', False, 'yolo or yolo-tiny')
flags.DEFINE_string('model', 'yolov4', 'yolov3 or yolov4')
flags.DEFINE_string('video', './data/video/test.mp4', 'path to input video or set to 0 for webcam')
flags.DEFINE_string('output', None, 'path to output video')
flags.DEFINE_string('output_format', 'XVID', 'codec used in VideoWriter when saving video to file')
flags.DEFINE_float('iou', 0.45, 'iou threshold')
flags.DEFINE_float('score', 0.50, 'score threshold')
flags.DEFINE_boolean('dont_show', False, 'dont show video output')
flags.DEFINE_boolean('info', False, 'show detailed info of tracked objects')
flags.DEFINE_boolean('count', False, 'count objects being tracked on screen')

class tf_lite_ngine:
    def __init__(self):
        # Initialize tf lite parameters
        self.interpreter = tf.lite.Interpreter(model_path=FLAGS.weights)
        self.interpreter.allocate_tensors()
        self.input_details = self.interpreter.get_input_details()
        self.output_details = self.interpreter.get_output_details()
        print(self.input_details)
        print(self.output_details)
    
    def detect(self, image_data):
        self.interpreter.set_tensor(self.input_details[0]['index'], image_data)
        self.interpreter.invoke()
        pred = [self.interpreter.get_tensor(self.output_details[i]['index']) 
                for i in range(len(self.output_details))]
        # run detections using yolov3 if flag is set
        if FLAGS.model == 'yolov3' and FLAGS.tiny == True:
            boxes, pred_conf = filter_boxes(pred[1], pred[0], score_threshold=0.25,
                                            input_shape=tf.constant([input_size, input_size]))
        else:
            boxes, pred_conf = filter_boxes(pred[0], pred[1], score_threshold=0.25,
                                            input_shape=tf.constant([input_size, input_size]))
        return boxes, pred_conf


def init_deepsort_params():
    # Definition of the parameters
    max_cosine_distance = 0.4
    nn_budget = None

    model_filename = 'model_data/mars-small128.pb'
    encoder = gdet.create_box_encoder(model_filename, batch_size=1)
    # calculate cosine distance metric
    metric = nn_matching.NearestNeighborDistanceMetric("cosine", max_cosine_distance, nn_budget)
    # initialize tracker
    tracker = Tracker(metric)
    return encoder, tracker
    
def load_obj_detector_cfg():
    config = ConfigProto()
    config.gpu_options.allow_growth = True
    session = InteractiveSession(config=config)
    STRIDES, ANCHORS, NUM_CLASS, XYSCALE = utils.load_config(FLAGS)
    input_size = FLAGS.size
    video_path = FLAGS.video
    return input_size, video_path

def init_video_out(vid):
    # by default VideoCapture returns float instead of int
    width = int(vid.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(vid.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = int(vid.get(cv2.CAP_PROP_FPS))
    codec = cv2.VideoWriter_fourcc(*FLAGS.output_format)
    out = cv2.VideoWriter(FLAGS.output, codec, fps, (width, height))
    return out

def print_fps(start_time, end_time, frame):
    fps = 1.0 / (end_time - start_time)
    print("FPS: %.2f" % fps)
    cv2.putText(frame, "FPS: %.2f" % fps, (5, 70), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 251, 0), 1)
    cv2.putText(frame, "GPU: NVIDIA GEFORCE GTX 960M", (5, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 251, 0), 1)

def apply_tf_nms(boxes, pred_conf):
    """
    Apply Tensorflow 2 non max suppression to input bounding boxes 
    """
    return tf.image.combined_non_max_suppression(
            boxes=tf.reshape(boxes, (tf.shape(boxes)[0], -1, 1, 4)),
            scores=tf.reshape(
                pred_conf, (tf.shape(pred_conf)[0], -1, tf.shape(pred_conf)[-1])),
            max_output_size_per_class=50,
            max_total_size=50,
            iou_threshold=FLAGS.iou,
            score_threshold=FLAGS.score
        )

def init_image_data(frame, input_size):
    image_data = cv2.resize(frame, (input_size, input_size))
    image_data = image_data / 255.
    image_data = image_data[np.newaxis, ...].astype(np.float32)
    return image_data

def get_allowed_obj_classes(classes, num_objects):
    # read in all class names from config
    class_names = utils.read_class_names(cfg.YOLO.CLASSES)

    # by default allow all classes in .names file
    #allowed_classes = list(class_names.values())
    allowed_classes = ['person', 'handbag', 'backpack', 'suitcase']
    
    # loop through objects and use class index to get class name, 
    # allow only classes in allowed_classes list
    names = []
    deleted_indx = []
    for i in range(num_objects):
        class_indx = int(classes[i])
        class_name = class_names[class_indx]
        if class_name not in allowed_classes:
            deleted_indx.append(i)
        else:
            names.append(class_name)
    names = np.array(names)
    return names, deleted_indx

def process_detections(tracker, detections, nms_max_overlap, frame):
    """
    Process objects detected by YOLO with deepsort
    Returns processed frame
    """
    #initialize color map
    cmap = plt.get_cmap('tab20b')
    colors = [cmap(i)[:3] for i in np.linspace(0, 1, 20)]

    # run non-maxima supression
    boxs = np.array([d.tlwh for d in detections])
    scores = np.array([d.confidence for d in detections])
    classes = np.array([d.class_name for d in detections])
    indices = preprocessing.non_max_suppression(boxs, classes, nms_max_overlap, scores)
    detections = [detections[i] for i in indices]       

    # Call the tracker
    tracker.predict()
    tracker.update(detections)

    # update tracks
    for track in tracker.tracks:
        if not track.is_confirmed() or track.time_since_update > 1:
            continue 
        bbox = track.to_tlbr()
        class_name = track.get_class()
        
        # draw bbox on screen
        color = colors[int(track.track_id) % len(colors)]
        color = [i * 255 for i in color]

        color1 = colors[int(track.track_id) % len(colors)]
        color1 = (103, 52, 219)

        color2 = colors[int(track.track_id) % len(colors)]
        color2 = (10, 247, 100)

        #cv2.rectangle(frame, (int(bbox[0]), int(bbox[1])), (int(bbox[2]), int(bbox[3])), color, 2)
        #cv2.rectangle(frame, (int(bbox[0]), int(bbox[1]-30)), (int(bbox[0])+(len(class_name)+len(str(track.track_id)))*17, int(bbox[1])), color, -1)
        #cv2.putText(frame, class_name + "-" + str(track.track_id),(int(bbox[0]), int(bbox[1]-10)),0, 0.75, (255,255,255),2)

        # only person, handbag, backpack and suitcase
        if class_name == 'person':
            cv2.rectangle(frame, (int(bbox[0]), int(bbox[1])), (int(bbox[2]), int(bbox[3])), color1, 2)
            cv2.rectangle(frame, (int(bbox[0]), int(bbox[1]-30)), (int(bbox[0])+(len(class_name)+len(str(track.track_id)))*17, int(bbox[1])), color1, -1)
            cv2.putText(frame, class_name + "-" + str(track.track_id),(int(bbox[0]), int(bbox[1]-10)),0, 0.75, (255,255,255),2)

        elif class_name == 'handbag' or 'backpack' or 'suitcase':
            cv2.rectangle(frame, (int(bbox[0]), int(bbox[1])), (int(bbox[2]), int(bbox[3])), color2, 2)
            cv2.rectangle(frame, (int(bbox[0]), int(bbox[1]-30)), (int(bbox[0])+(len(class_name)+len(str(track.track_id)))*17, int(bbox[1])), color2, -1)
            cv2.putText(frame, class_name + "-" + str(track.track_id),(int(bbox[0]), int(bbox[1]-10)),0, 0.75, (255,255,255),2)


        # if enable info flag then print details about each track
        if FLAGS.info:
            print("Tracker ID: {}, Class: {},  BBox Coords (xmin, ymin, xmax, ymax): {}".format(str(track.track_id), 
                class_name, (int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3]))))
    return frame


def get_video_stream(video_path):
    try:
        vid = cv2.VideoCapture(int(video_path))
    except:
        vid = cv2.VideoCapture(video_path)
    return vid

def show_tracked_object_count(names, frame):
    count = len(names)
    cv2.putText(frame, "Objects being tracked: {}".format(count), (5, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 251, 0), 1)
    print("Objects being tracked: {}".format(count))

def detections_to_np_array(detections, boxes, scores, classes):
    num_objects = detections.numpy()[0]
    bboxes = boxes.numpy()[0]
    bboxes = bboxes[0:int(num_objects)]
    scores = scores.numpy()[0]
    scores = scores[0:int(num_objects)]
    classes = classes.numpy()[0]
    classes = classes[0:int(num_objects)]
    return num_objects, bboxes, scores, classes


def main(_argv):
    # Definition of the parameters
    nms_max_overlap = 1.0
    
    # initialize deep sort parameters
    encoder, tracker = init_deepsort_params()

    # load configuration for object detector
    input_size, video_path = load_obj_detector_cfg()

    # load tflite model if flag is set
    if FLAGS.framework == 'tflite':
        tfl = tf_lite_ngine()
    # otherwise load standard tensorflow saved model
    else:
        saved_model_loaded = tf.saved_model.load(FLAGS.weights, tags=[tag_constants.SERVING])
        infer = saved_model_loaded.signatures['serving_default']

    # begin video capture
    vid = get_video_stream(video_path)

    # get video ready to save locally if flag is set
    out = None
    if FLAGS.output:
        out = init_video_out(vid)

    frame_num = 0
    # while video is running
    while True:
        return_value, frame = vid.read()
        if return_value:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            image = Image.fromarray(frame)
        else:
            print('Video has ended or failed, try a different video format!')
            break

        frame_num +=1
        print('Frame #: ', frame_num)
    
        frame_size = frame.shape[:2]
        image_data = init_image_data(frame, input_size)
        start_time = time.time()

        # run detections on tflite if flag is set
        if FLAGS.framework == 'tflite':
            boxes, pred_conf = tfl.detect(image_data)
        else:
            batch_data = tf.constant(image_data)
            pred_bbox = infer(batch_data)
            for key, value in pred_bbox.items():
                boxes = value[:, :, 0:4]
                pred_conf = value[:, :, 4:]

        boxes, scores, classes, valid_detections = apply_tf_nms(boxes, pred_conf) 

        # convert data to numpy arrays and slice out unused elements
        num_objects, bboxes, scores, classes = detections_to_np_array(valid_detections, 
                                                                    boxes, scores, classes)

        # format bounding boxes from normalized ymin, xmin, ymax, xmax ---> xmin, ymin, width, height
        original_h, original_w, _ = frame.shape
        bboxes = utils.format_boxes(bboxes, original_h, original_w)

        # fetch allowed object classes, ignore the rest classes
        names, deleted_indx = get_allowed_obj_classes(classes, num_objects)
        if FLAGS.count:
            show_tracked_object_count(names, frame)

        # delete detections that are not in allowed_classes
        bboxes = np.delete(bboxes, deleted_indx, axis=0)
        scores = np.delete(scores, deleted_indx, axis=0)

        # encode yolo detections and feed to tracker
        features = encoder(frame, bboxes)
        detections = [Detection(bbox, score, class_name, feature) 
                    for bbox, score, class_name, feature in zip(bboxes, scores, names, features)]

        # process detections with YOLO tracker
        frame = process_detections(tracker, detections, nms_max_overlap, frame)

        # calculate and print frames per second of running detections
        print_fps(start_time, time.time(), frame)
        result = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
        
        if not FLAGS.dont_show:
            cv2.imshow("Output Video", result)
        
        # if output flag is set, save video file
        if FLAGS.output:
            out.write(result)
        if cv2.waitKey(1) & 0xFF == ord('q'): break
    cv2.destroyAllWindows()

if __name__ == '__main__':
    try:
        app.run(main)
    except SystemExit:
        pass