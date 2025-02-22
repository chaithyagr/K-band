#!/usr/bin/env python

from test_tube import HyperOptArgumentParser
from pytorch_lightning import Trainer
from pytorch_lightning.loggers import TestTubeLogger
from pytorch_lightning.callbacks import ModelCheckpoint

from pytorch_lightning import seed_everything

import pathlib
import argparse
import time
import sys

from deepinpy.recons import CGSenseRecon, MoDLRecon, ResNetRecon, DeepBasisPursuitRecon
from deepinpy.forwards import MultiChannelMRIDataset

import torch

torch.backends.cudnn.enabled = True
torch.backends.cudnn.benchmark = True

import random  # used to avoid race conditions, intentionall unseeded
import numpy.random
import numpy as np


def main_train(args, gpu_ids=None):
    if args.hyperopt:
        time.sleep(random.random())  # used to avoid race conditions with parallel jobs
    tt_logger = TestTubeLogger(
        save_dir=args.logdir,
        name=args.name,
        debug=False,
        create_git_tag=False,
        version=args.version,
        log_graph=False,
    )
    tt_logger.log_hyperparams(args)
    save_path = "./{}/{}/version_{}".format(
        args.logdir, tt_logger.name, tt_logger.version
    )
    print("save path is", save_path)
    checkpoint_path = "{}/checkpoints".format(save_path)
    pathlib.Path(checkpoint_path).mkdir(parents=True, exist_ok=True)
    if args.save_all_checkpoints:
        save_top_k = -1
    else:
        save_top_k = 1
    checkpoint_callback = ModelCheckpoint(
        checkpoint_path, "epoch", save_top_k=save_top_k, mode="max", verbose=False
    )

    if args.recon == "cgsense":
        MyRecon = CGSenseRecon
    elif args.recon == "modl":
        MyRecon = MoDLRecon
    elif args.recon == "resnet":
        MyRecon = ResNetRecon
    elif args.recon == "dbp":
        MyRecon = DeepBasisPursuitRecon
    else:
        print("error! undefined recon")
        sys.exit(-1)

    M = MyRecon(args)

    # Load checkpoint epoch to reconstruct images from.
    if args.checkpoint_init:
        print("loading checkpoint: {}".format(args.checkpoint_init))
        checkpoint = torch.load(
            args.checkpoint_init, map_location=lambda storage, loc: storage
        )
        M.load_state_dict(checkpoint["state_dict"])

    # Train from scratch.
    print("training from scratch")
    if gpu_ids is None:
        gpus = None
        accelerator = None
    else:
        gpus = gpu_ids
        accelerator = None
    trainer = Trainer(
        max_epochs=args.num_epochs,
        gpus=gpus,
        logger=tt_logger,
        checkpoint_callback=checkpoint_callback,
        accelerator=accelerator,
        accumulate_grad_batches=args.num_accumulate,
        progress_bar_refresh_rate=1,
        gradient_clip_val=args.clip_grads,
    )
    trainer.fit(M)

    M.eval()
    with torch.no_grad():
        if args.data_inference_file:
            # Iterate through specified files and reconstruct k-space using network.
            for i in range(len(args.data_inference_file)):
                eval_data = MultiChannelMRIDataset(
                    data_file=args.data_inference_file[i],
                    masks_file=args.masks_inference_file[i],
                    stdev=args.stdev,
                    num_data_sets=args.num_inference_data_sets,
                    adjoint_data=args.adjoint_data,
                    id=0,
                    clear_cache=False,
                    cache_data=False,
                    scale_data=False,
                    fully_sampled=args.fully_sampled,
                    data_idx=None,
                    inverse_crime=args.inverse_crime,
                    noncart=args.noncart,
                )
                eval_loader = torch.utils.data.DataLoader(
                    eval_data,
                    batch_size=args.num_inference_data_sets,
                    shuffle=False,
                    num_workers=0,
                    drop_last=True,
                )
                for batch in eval_loader:
                    M.batch(batch[1])
                    recon_imgs = M(batch[1]["out"])
                    print(recon_imgs.shape)
                    np.save(
                        args.masks_train_file + "_" + args.loss_function + ".npy",
                        recon_imgs,
                    )


if __name__ == "__main__":
    usage_str = "usage: %(prog)s [options]"
    description_str = "deep inverse problems optimization"

    parser = HyperOptArgumentParser(
        usage=usage_str,
        description=description_str,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        strategy="random_search",
    )

    parser.opt_range(
        "--step",
        type=float,
        dest="step",
        default=0.001,
        help="step size/learning rate",
        tunable=True,
        nb_samples=100,
        low=0.0001,
        high=0.001,
    )
    parser.opt_range(
        "--l2lam_init",
        action="store",
        type=float,
        dest="l2lam_init",
        default=0.001,
        tunable=False,
        low=0.0001,
        high=100,
        help="initial l2 regularization",
    )
    parser.opt_list(
        "--solver",
        action="store",
        dest="solver",
        type=str,
        tunable=False,
        options=["sgd", "adam"],
        help='optimizer/solver ("adam", "sgd")',
        default="adam",
    )
    parser.opt_range(
        "--cg_max_iter",
        action="store",
        dest="cg_max_iter",
        type=int,
        tunable=False,
        low=1,
        high=20,
        help="max number of conjgrad iterations",
        default=10,
    )
    parser.add_argument(
        "--R_band",
        action="store",
        dest="R_band",
        type=int,
        help="Acceleration factor of kband width",
        default=3,
    )
    parser.opt_range(
        "--batch_size",
        action="store",
        dest="batch_size",
        type=int,
        tunable=False,
        low=1,
        high=20,
        help="batch size",
        default=1,
    )
    parser.opt_range(
        "--num_unrolls",
        action="store",
        dest="num_unrolls",
        type=int,
        tunable=False,
        low=1,
        high=10,
        nb_samples=4,
        help="number of unrolls",
        default=4,
    )
    parser.opt_range(
        "--num_admm",
        action="store",
        dest="num_admm",
        type=int,
        tunable=False,
        low=1,
        high=10,
        nb_samples=4,
        help="number of ADMM iterations",
        default=3,
    )
    parser.opt_list(
        "--network",
        action="store",
        dest="network",
        type=str,
        tunable=False,
        options=["ResNet", "ResNet5Block"],
        help="which denoiser network to use",
        default="ResNet",
    )
    parser.opt_list(
        "--latent_channels",
        action="store",
        dest="latent_channels",
        type=int,
        tunable=False,
        options=[16, 32, 64, 128],
        help="number of latent channels",
        default=64,
    )
    parser.opt_range(
        "--num_blocks",
        action="store",
        dest="num_blocks",
        type=int,
        tunable=False,
        low=1,
        high=4,
        nb_samples=3,
        help="number of ResNetBlocks",
        default=3,
    )
    parser.opt_range(
        "--dropout",
        action="store",
        dest="dropout",
        type=float,
        tunable=False,
        low=0.0,
        high=0.5,
        help="dropout fraction",
        default=0.0,
    )
    parser.opt_list(
        "--batch_norm",
        action="store_true",
        dest="batch_norm",
        tunable=False,
        options=[True, False],
        help="batch normalization",
        default=False,
    )

    parser.add_argument(
        "--num_accumulate",
        action="store",
        dest="num_accumulate",
        type=int,
        help="nunumber of batch accumulations",
        default=1,
    )
    parser.add_argument(
        "--name",
        action="store",
        dest="name",
        type=str,
        help="experiment name",
        default=1,
    )
    parser.add_argument(
        "--version",
        action="store",
        dest="version",
        type=int,
        help="version number",
        default=None,
    )
    parser.add_argument(
        "--gpu",
        action="store",
        dest="gpu",
        type=str,
        help="gpu number(s)",
        default=None,
    )
    parser.add_argument(
        "--num_epochs",
        action="store",
        dest="num_epochs",
        type=int,
        help="number of epochs",
        default=20,
    )
    parser.add_argument(
        "--random_seed",
        action="store",
        dest="random_seed",
        type=int,
        help="random number seed for numpy",
        default=723,
    )
    parser.add_argument(
        "--recon",
        action="store",
        type=str,
        dest="recon",
        default="cgsense",
        help="reconstruction method",
    )
    parser.add_argument(
        "--data_train_file",
        action="store",
        dest="data_train_file",
        type=str,
        help="data.h5",
        default=None,
    )
    parser.add_argument(
        "--data_val_file",
        action="store",
        dest="data_val_file",
        type=str,
        help="val data.h5",
        default=None,
    )
    parser.add_argument(
        "--data_inference_file",
        action="store",
        dest="data_inference_file",
        nargs="+",
        type=str,
        help="test data.h5",
        default=None,
    )
    parser.add_argument(
        "--num_train_data_sets",
        action="store",
        dest="num_train_data_sets",
        type=int,
        help="number of data sets to use",
        default=None,
    )
    parser.add_argument(
        "--num_val_data_sets",
        action="store",
        dest="num_recon_data_sets",
        type=int,
        help="number of val data sets to use",
        default=None,
    )
    parser.add_argument(
        "--num_recon_data_sets",
        action="store",
        dest="num_test_data_sets",
        type=int,
        help="number of recon data sets to use",
        default=None,
    )
    parser.add_argument(
        "--loss_function",
        action="store",
        dest="loss_function",
        type=str,
        help="loss function",
        default="L1",
    )
    parser.add_argument(
        "--num_workers",
        action="store",
        type=int,
        dest="num_workers",
        help="number of workers",
        default=0,
    )
    parser.add_argument(
        "--shuffle",
        action="store_true",
        dest="shuffle",
        help="shuffle input data files each epoch",
        default=False,
    )
    parser.add_argument(
        "--clip_grads",
        action="store",
        type=float,
        dest="clip_grads",
        help="clip norm of gradient vector to val",
        default=0,
    )
    parser.add_argument(
        "--cg_eps",
        action="store",
        type=float,
        dest="cg_eps",
        help="conjgrad eps",
        default=1e-4,
    )
    parser.add_argument(
        "--stdev",
        action="store",
        type=float,
        dest="stdev",
        help="complex valued noise standard deviation",
        default=0.0,
    )
    parser.add_argument(
        "--max_norm_constraint",
        action="store",
        type=float,
        dest="max_norm_constraint",
        help="norm constraint on weights",
        default=None,
    )
    parser.add_argument(
        "--fully_sampled",
        action="store_true",
        dest="fully_sampled",
        help="fully_sampled",
        default=False,
    )
    parser.add_argument(
        "--adam_eps",
        action="store",
        type=float,
        dest="adam_eps",
        help="adam epsilon",
        default=1e-8,
    )
    parser.add_argument(
        "--inverse_crime",
        action="store_true",
        dest="inverse_crime",
        help="inverse crime",
        default=False,
    )
    parser.add_argument(
        "--use_sigpy",
        action="store_true",
        dest="use_sigpy",
        help="use SigPy for Linops",
        default=False,
    )
    parser.add_argument(
        "--noncart",
        action="store_true",
        dest="noncart",
        help="NonCartesian data",
        default=False,
    )
    parser.add_argument(
        "--abs_loss",
        action="store_true",
        dest="abs_loss",
        help="use magnitude for loss",
        default=False,
    )
    parser.add_argument(
        "--use_W_mask",
        action="store_true",
        dest="use_W_mask",
        help="use W (density compensation) mask during loss calculation",
        default=True,
    )
    parser.add_argument(
        "--self_supervised",
        action="store_true",
        dest="self_supervised",
        help="self-supervised loss",
        default=False,
    )
    parser.add_argument(
        "--hyperopt",
        action="store_true",
        dest="hyperopt",
        help="perform hyperparam optimization",
        default=False,
    )
    parser.add_argument(
        "--checkpoint_init",
        action="store",
        dest="checkpoint_init",
        type=str,
        help="load from checkpoint",
        default=None,
    )
    parser.add_argument(
        "--logdir",
        action="store",
        dest="logdir",
        type=str,
        help="log dir",
        default="logs",
    )
    parser.add_argument(
        "--save_all_checkpoints",
        action="store_true",
        dest="save_all_checkpoints",
        help="Save all checkpoints",
        default=True,
    )
    parser.add_argument(
        "--lr_scheduler",
        action="store",
        dest="lr_scheduler",
        nargs="+",
        type=int,
        help="do [#epoch, learning rate multiplicative factor] to use a learning rate scheduler",
        default=-1,
    )
    parser.add_argument(
        "--save_every_N_epochs",
        action="store",
        type=int,
        dest="save_every_N_epochs",
        help="save images every N epochs",
        default=1,
    )
    parser.add_argument(
        "--num_spatial_dimensions",
        action="store",
        dest="num_spatial_dimensions",
        type=int,
        help="num of spatial dimensions in ksp.shape, e.g. (..., Nx, Ny, Nz, ...) means 3 spatial dimensions. Currently 2D or 3D is supported",
        default=2,
    )
    parser.opt_list(
        "--adjoint_data",
        action="store_true",
        dest="adjoint_data",
        tunable=False,
        options=[True, False],
        help="Work with adjoint of data",
        default=True,
    )
    parser.json_config("--config", default=None)

    args = parser.parse_args()

    torch.manual_seed(args.random_seed)
    numpy.random.seed(args.random_seed)
    seed_everything(args.random_seed)

    if args.hyperopt:
        if args.gpu is None:
            args.optimize_parallel_cpu(
                main_train, nb_trials=args.num_trials, nb_workers=args.num_workers
            )
        else:
            gpu_ids = [a.strip() for a in args.gpu.split(",")]
            args.optimize_parallel_gpu(
                main_train, gpu_ids=gpu_ids, max_nb_trials=args.num_trials
            )
    else:
        if args.gpu is not None:
            gpu_ids = [int(a) for a in args.gpu.split(",")]
        else:
            gpu_ids = None
        main_train(args, gpu_ids=gpu_ids)
