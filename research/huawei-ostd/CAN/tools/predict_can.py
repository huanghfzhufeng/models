"""
Text recognition inference
"""
import logging
import os
import sys
from time import time

import numpy as np
import mindspore as ms
import mindspore.ops as ops
from mindspore.common import dtype as mstype

from config import parse_args
from postprocess import Postprocessor
from preprocess import Preprocessor
from utils import get_ckpt_file, get_image_paths

__dir__ = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(__dir__, "../../../")))

from can import build_model
from can.utils.logger import set_logger
from can.utils.visualize import show_imgs

# map algorithm name to model name (which can be checked by `mindocr.list_models()`)
# NOTE: Modify it to add new model for inference.
algo_to_model_name = {
    "CAN": "can"
}
logger = logging.getLogger("mindocr")


class TextRecognizer(object):
    def __init__(self, args):
        self.batch_num = args.rec_batch_num
        self.batch_mode = args.rec_batch_mode
        # self.batch_mode = args.rec_batch_mode and (self.batch_num > 1)
        logger.info(
            "recognize in {} mode {}".format(
                "batch" if self.batch_mode else "serial",
                "batch_size: " + str(self.batch_num) if self.batch_mode else "",
            )
        )

        # build model for algorithm with pretrained weights or local checkpoint
        ckpt_dir = args.rec_model_dir
        if ckpt_dir is None:
            pretrained = True
            ckpt_load_path = None
        else:
            ckpt_load_path = get_ckpt_file(ckpt_dir)
            pretrained = False
        assert args.rec_algorithm in algo_to_model_name, (
            f"Invalid rec_algorithm {args.rec_algorithm}. "
            f"Supported recognition algorithms are {list(algo_to_model_name.keys())}"
        )
        # model_name = algo_to_model_name[args.rec_algorithm]
        model_name = self.get_can_config()

        amp_level = args.rec_amp_level
        if args.rec_algorithm.startswith("SVTR") and amp_level != "O2":
            logger.warning(
                "SVTR recognition model is optimized for amp_level O2. ampl_level for rec model is changed to O2"
            )
            amp_level = "O2"
        if ms.get_context("device_target") == "GPU" and amp_level == "O3":
            logger.warning(
                "Recognition model prediction does not support amp_level O3 on GPU currently. "
                "The program has switched to amp_level O2 automatically."
            )
            amp_level = "O2"
        self.model = build_model(model_name, pretrained=pretrained, ckpt_load_path=ckpt_load_path, amp_level=amp_level)

        self.model.set_train(False)
        self.cast_pred_fp32 = amp_level != "O0"
        if self.cast_pred_fp32:
            self.cast = ops.Cast()
        logger.info(
            "Init recognition model: {} --> {}. Model weights loaded from {}".format(
                args.rec_algorithm, model_name, "pretrained url" if pretrained else ckpt_load_path
            )
        )

        self.preprocess = Preprocessor(
            task="rec",
            algo=args.rec_algorithm,
            rec_image_shape=args.rec_image_shape,
            rec_batch_mode=self.batch_mode,
            rec_batch_num=self.batch_num,
        )

        self.postprocess = Postprocessor(
            task="rec", algo=args.rec_algorithm, rec_char_dict_path=args.rec_char_dict_path
        )

        self.vis_dir = args.draw_img_save_dir
        os.makedirs(self.vis_dir, exist_ok=True)

    def __call__(self, img_or_path_list: list, do_visualize=False):
        """
        Run text recognition serially for input images

        Args:
            img_or_path_list: list of str for img path or np.array for RGB image
            do_visualize: visualize preprocess and final result and save them

        Return:
            list of dict, each contains the follow keys for recognition result.
            e.g. [{'texts': 'abc', 'confs': 0.9}, {'texts': 'cd', 'confs': 1.0}]
                - texts: text string
                - confs: prediction confidence
        """

        assert isinstance(img_or_path_list, list), "Input for text recognition must be list of images or image paths."
        logger.info(f"num images for rec: {len(img_or_path_list)}")
        if self.batch_mode:
            rec_res_all_crops = self.run_batchwise(img_or_path_list, do_visualize)
        else:
            rec_res_all_crops = []
            for i, img_or_path in enumerate(img_or_path_list):
                rec_res = self.run_single(img_or_path, i, do_visualize)
                rec_res_all_crops.append(rec_res)

        return rec_res_all_crops
    
    def get_can_config(self):
        model_config = {
            "backbone": {
                "name": "rec_can_densenet",
                "pretrained": False,
                "growth_rate": 24, 
                "reduction": 0.5, 
                "bottleneck": True, 
                "use_dropout": True,
                "input_channels": 1,
                },
            "head": {
                "name": "CANHead",
                "out_channels": 111,
                "ratio": 16,
                "attdecoder": {
                    "is_train": False,
                    "input_size": 256,
                    "hidden_size": 256,
                    "encoder_out_channel": 684,
                    "dropout": True,
                    "dropout_ratio": 0.5,
                    "word_num": 111,
                    "counting_decoder_out_channel": 111,
                    "attention": {
                            "attention_dim": 512,
                            "word_conv_kernel": 1,
                        },
                    },
                },
        }
        return model_config

    def run_batchwise(self, img_or_path_list: list, do_visualize=False):
        """
        Run text recognition serially for input images

                Args:
            img_or_path_list: list of str for img path or np.array for RGB image
            do_visualize: visualize preprocess and final result and save them

                Return:
            rec_res: list of tuple, where each tuple is  (text, score) - text recognition result for each input image
                in order.
                    where text is the predicted text string, score is its confidence score.
                    e.g. [('apple', 0.9), ('bike', 1.0)]
        """
        rec_res = []
        num_imgs = len(img_or_path_list)

        for idx in range(0, num_imgs, self.batch_num):  # batch begin index i
            batch_begin = idx
            batch_end = min(idx + self.batch_num, num_imgs)
            logger.info(f"Rec img idx range: [{batch_begin}, {batch_end})")

            # preprocess
            # TODO: run in parallel with multiprocessing
            img_batch = []
            data = {}
            for j in range(batch_begin, batch_end):  # image index j
                data = self.preprocess(img_or_path_list[j])
                img_batch.append(data["image"])
                if do_visualize:
                    fn = os.path.basename(data.get("img_path", f"crop_{j}.png")).rsplit(".", 1)[0]
                    show_imgs(
                        [data["image"]],
                        title=fn + "_rec_preprocessed",
                        mean_rgb=[127.0, 127.0, 127.0],
                        std_rgb=[127.0, 127.0, 127.0],
                        is_chw=True,
                        show=False,
                        save_path=os.path.join(self.vis_dir, fn + "_rec_preproc.png"),
                    )

            img_batch = np.stack(img_batch) if len(img_batch) > 1 else np.expand_dims(img_batch[0], axis=0)

            image_mask = ops.ones(img_batch.shape, dtype=ms.float32)
            label = ops.ones((1, 36), dtype=ms.int64)

            image = ms.Tensor(img_batch)

            net_pred = self.model(image,image_mask,label)
            if self.cast_pred_fp32:
                if isinstance(net_pred, list) or isinstance(net_pred, tuple):
                    net_pred = [self.cast(p, mstype.float32) for p in net_pred]
                else:
                    net_pred = self.cast(net_pred, mstype.float32)

            # postprocess
            batch_res = self.postprocess(net_pred)

            rec_res = batch_res["texts"]

        return rec_res

    def run_single(self, img_or_path, crop_idx=0, do_visualize=True):
        """
        Text recognition inference on a single image
        Args:
            img_or_path: str for image path or np.array for image rgb value

        Return:
            dict with keys:
                - texts (str): preditive text string
                - confs (int): confidence of the prediction
        """
        # preprocess
        data = self.preprocess(img_or_path)

        # visualize preprocess result
        if do_visualize:
            # show_imgs([data['image_ori']], is_bgr_img=False, title=f'origin_{i}')
            fn = os.path.basename(data.get("img_path", f"crop_{crop_idx}.png")).rsplit(".", 1)[0]
            show_imgs(
                [data["image"]],
                title=fn + "_rec_preprocessed",
                mean_rgb=[127.0, 127.0, 127.0],
                std_rgb=[127.0, 127.0, 127.0],
                is_chw=True,
                show=False,
                save_path=os.path.join(self.vis_dir, fn + "_rec_preproc.png"),
            )
        logger.info(f"Origin image shape: {data['image_ori'].shape}")
        logger.info(f"Preprocessed image shape: {data['image'].shape}")

        # infer
        input_np = data["image"]
        if len(input_np.shape) == 3:
            net_input = np.expand_dims(input_np, axis=0)

        net_pred = self.model(ms.Tensor(net_input))
        if self.cast_pred_fp32:
            if isinstance(net_pred, list) or isinstance(net_pred, tuple):
                net_pred = [self.cast(p, mstype.float32) for p in net_pred]
            else:
                net_pred = self.cast(net_pred, mstype.float32)

        # postprocess
        rec_res = self.postprocess(net_pred)
        # if 'raw_chars' in rec_res:
        #    rec_res.pop('raw_chars')

        rec_res = (rec_res["texts"][0], rec_res["confs"][0])

        logger.info(f"Crop {crop_idx} rec result: {rec_res}")

        return rec_res


def save_rec_res(rec_res_all, img_paths, include_score=False, save_path="./rec_results.txt"):
    lines = []
    for i, rec_res in enumerate(rec_res_all):
        if include_score:
            img_pred = os.path.basename(img_paths[i]) + "\t" + rec_res[0] + "\t" + rec_res[1] + "\n"
        else:
            img_pred = os.path.basename(img_paths[i]) + "\t" + rec_res[0] + "\n"
        lines.append(img_pred)

    with open(save_path, "w") as f:
        f.writelines(lines)
        f.close()

    return lines


if __name__ == "__main__":
    # parse args
    args = parse_args()
    set_logger(name="mindocr")
    save_dir = args.draw_img_save_dir
    img_paths = get_image_paths(args.image_dir)
    # uncomment it to quick test the infer FPS
    # img_paths = img_paths[:250]

    ms.set_context(mode=args.mode)

    # init detector
    text_recognize = TextRecognizer(args)

    # TODO: warmup

    # run for each image
    start = time()
    rec_res_all = text_recognize(img_paths, do_visualize=False)
    t = time() - start
    # save all results in a txt file
    save_fp = os.path.join(save_dir, "rec_results.txt" if args.rec_batch_mode else "rec_results_serial.txt")
    save_rec_res(rec_res_all, img_paths, save_path=save_fp)
    logger.info(f"All rec res: {rec_res_all}")
    logger.info(f"Done! Text recognition results saved in {save_dir}")
    logger.info(f"Time cost: {t}, FPS: {len(img_paths) / t}")
