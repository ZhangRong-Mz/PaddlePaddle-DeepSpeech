"""Beam search parameters tuning for DeepSpeech2 model."""

import numpy as np
import argparse
import functools
import paddle.fluid as fluid
from tqdm import tqdm
from data_utils.data import DataGenerator
from model_utils.model import DeepSpeech2Model
from utils.error_rate import char_errors, word_errors
from utils.utility import add_arguments, print_arguments

parser = argparse.ArgumentParser(description=__doc__)
add_arg = functools.partial(add_arguments, argparser=parser)
add_arg('num_batches',      int,   -1,   "# of batches tuning on. Default -1, on whole dev set.")
add_arg('batch_size',       int,   64,   "# of samples per batch.")
add_arg('beam_size',        int,   10,   "Beam search width. range:[5, 500]")
add_arg('num_proc_bsearch', int,   8,    "# of CPUs for beam search.")
add_arg('num_conv_layers',  int,   2,    "# of convolution layers.")
add_arg('num_rnn_layers',   int,   3,    "# of recurrent layers.")
add_arg('rnn_layer_size',   int,   2048, "# of recurrent cells per layer.")
add_arg('num_alphas',       int,   45,   "# of alpha candidates for tuning.")
add_arg('num_betas',        int,   8,    "# of beta candidates for tuning.")
add_arg('alpha_from',       float, 1.0,  "Where alpha starts tuning from.")
add_arg('alpha_to',         float, 3.2,  "Where alpha ends tuning with.")
add_arg('beta_from',        float, 0.1,  "Where beta starts tuning from.")
add_arg('beta_to',          float, 0.45, "Where beta ends tuning with.")
add_arg('cutoff_prob',      float, 1.0,  "Cutoff probability for pruning.")
add_arg('cutoff_top_n',     int,   40,   "Cutoff number for pruning.")
add_arg('use_gru',          bool,  True, "Use GRUs instead of simple RNNs.")
add_arg('use_gpu',          bool,  True,  "Use GPU or not.")
add_arg('share_rnn_weights', bool, False,  "Share input-hidden weights across bi-directional RNNs. Not for GRU.")
add_arg('tune_manifest',     str,  './dataset/manifest.dev',   "Filepath of manifest to tune.")
add_arg('mean_std_path',     str,  './dataset/mean_std.npz',   "Filepath of normalizer's mean & std.")
add_arg('vocab_path',        str,  './dataset/zh_vocab.txt',   "Filepath of vocabulary.")
add_arg('lang_model_path',   str,  './lm/zh_giga.no_cna_cmn.prune01244.klm', "Filepath for language model.")
add_arg('model_path',        str,  './models/step_final',
        "If None, the training starts from scratch, otherwise, it resumes from the pre-trained model.")
add_arg('error_rate_type',   str,  'cer',    "Error rate type for evaluation.", choices=['wer', 'cer'])
add_arg('specgram_type',     str,  'linear', "Audio feature type. Options: linear, mfcc.", choices=['linear', 'mfcc'])
args = parser.parse_args()


def tune():
    # 逐步调整alphas参数和betas参数
    if not args.num_alphas >= 0:
        raise ValueError("num_alphas must be non-negative!")
    if not args.num_betas >= 0:
        raise ValueError("num_betas must be non-negative!")

    # 是否使用GPU
    if args.use_gpu:
        place = fluid.CUDAPlace(0)
    else:
        place = fluid.CPUPlace()

    # 获取数据生成器
    data_generator = DataGenerator(vocab_filepath=args.vocab_path,
                                   mean_std_filepath=args.mean_std_path,
                                   augmentation_config='{}',
                                   specgram_type=args.specgram_type,
                                   keep_transcription_text=True,
                                   place=place,
                                   is_training=False)
    # 获取评估数据
    batch_reader = data_generator.batch_reader_creator(manifest_path=args.tune_manifest,
                                                       batch_size=args.batch_size,
                                                       sortagrad=False,
                                                       shuffle_method=None)
    # 获取DeepSpeech2模型，并设置为预测
    ds2_model = DeepSpeech2Model(vocab_size=data_generator.vocab_size,
                                 num_conv_layers=args.num_conv_layers,
                                 num_rnn_layers=args.num_rnn_layers,
                                 rnn_layer_size=args.rnn_layer_size,
                                 use_gru=args.use_gru,
                                 place=place,
                                 init_from_pretrained_model=args.model_path,
                                 share_rnn_weights=args.share_rnn_weights,
                                 is_infer=True)

    # 获取评估函数，有字错率和词错率
    errors_func = char_errors if args.error_rate_type == 'cer' else word_errors
    # 创建用于搜索的alphas参数和betas参数
    cand_alphas = np.linspace(args.alpha_from, args.alpha_to, args.num_alphas)
    cand_betas = np.linspace(args.beta_from, args.beta_to, args.num_betas)
    params_grid = [(alpha, beta) for alpha in cand_alphas for beta in cand_betas]

    err_sum = [0.0 for i in range(len(params_grid))]
    err_ave = [0.0 for i in range(len(params_grid))]
    num_ins, len_refs, cur_batch = 0, 0, 0
    # 初始化定向搜索方法
    ds2_model.init_ext_scorer(args.alpha_from, args.beta_from, args.lang_model_path, data_generator.vocab_list)
    # 多批增量调优参数
    ds2_model.logger.info("start tuning ...")
    for infer_data in batch_reader():
        if (args.num_batches >= 0) and (cur_batch >= args.num_batches):
            break
        # 执行预测
        probs_split = ds2_model.infer_batch_probs(infer_data=infer_data)
        target_transcripts = infer_data[1]

        num_ins += len(target_transcripts)
        # 搜索alphas参数和betas参数
        for index, (alpha, beta) in enumerate(tqdm(params_grid)):
            result_transcripts = ds2_model.decode_batch_beam_search(probs_split=probs_split,
                                                                    beam_alpha=alpha,
                                                                    beam_beta=beta,
                                                                    beam_size=args.beam_size,
                                                                    cutoff_prob=args.cutoff_prob,
                                                                    cutoff_top_n=args.cutoff_top_n,
                                                                    vocab_list=data_generator.vocab_list,
                                                                    num_processes=args.num_proc_bsearch)
            for target, result in zip(target_transcripts, result_transcripts):
                errors, len_ref = errors_func(target, result)
                err_sum[index] += errors
                if args.alpha_from == alpha and args.beta_from == beta:
                    len_refs += len_ref

            err_ave[index] = err_sum[index] / len_refs

        # 输出每一个batch的计算结果
        err_ave_min = min(err_ave)
        min_index = err_ave.index(err_ave_min)
        print("\nBatch %d [%d/?], current opt (alpha, beta) = (%s, %s), "
              " min [%s] = %f" % (cur_batch, num_ins, "%.3f" % params_grid[min_index][0],
                                  "%.3f" % params_grid[min_index][1], args.error_rate_type, err_ave_min))
        cur_batch += 1

    # 输出字错率和词错率以及(alpha, beta)
    print("\nFinal %s:\n" % args.error_rate_type)
    for index in range(len(params_grid)):
        print("(alpha, beta) = (%s, %s), [%s] = %f"
              % ("%.3f" % params_grid[index][0], "%.3f" % params_grid[index][1], args.error_rate_type, err_ave[index]))

    err_ave_min = min(err_ave)
    min_index = err_ave.index(err_ave_min)
    print("\nFinish tuning on %d batches, final opt (alpha, beta) = (%s, %s)"
          % (cur_batch, "%.3f" % params_grid[min_index][0], "%.3f" % params_grid[min_index][1]))

    ds2_model.logger.info("finish tuning")


def main():
    print_arguments(args)
    tune()


if __name__ == '__main__':
    main()
