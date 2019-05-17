import copy
import re

import numpy as np
import torch
from torch import nn
from torch.nn import functional as F

from parlai.core.torch_agent import Output, TorchAgent
from parlai.core.utils import round_sigfigs

from .modules import AutoRec, ReconstructionLoss


class AutorecTagAgent(TorchAgent):
    @classmethod
    def add_cmdline_args(cls, argparser):
        """Add command-line arguments specifically for this agent."""
        super(AutorecTagAgent, cls).add_cmdline_args(argparser)
        agent = argparser.add_argument_group("Autorec Arguments")
        agent.add_argument(
            "-nmv", "--n-movies", type=int, help="number of movies in the dataset"
        )
        agent.add_argument(
            "-hs",
            "--hiddensize",
            type=int,
            default=1000,
            help="size of the hidden layers",
        )
        agent.add_argument(
            "-lr", "--learningrate", type=float, default=0.0001, help="learning rate"
        )
        agent.add_argument(
            "--gpu", type=int, default=-1, help="which GPU device to use"
        )
        AutorecTagAgent.dictionary_class().add_cmdline_args(argparser)
        return agent

    def __init__(self, opt, shared=None):
        super().__init__(opt, shared)
        init_model, is_finetune = self._get_init_model(opt, shared)

        self.id = "AutorecTagAgent"
        self.n_movies = opt["n_movies"]

        if not shared:
            # set up model from scratch
            nmv = opt["n_movies"]
            hsz = opt["hiddensize"]

            # encoder captures the input text
            self.model = AutoRec(
                nmv, params={"layer_sizes": [hsz], "f": "sigmoid", "g": "sigmoid"}
            )
            if init_model is not None:
                # load model parameters if available
                print("[ Loading existing model params from {} ]" "".format(init_model))
                states = self.load(init_model)
                if "number_training_updates" in states:
                    self._number_training_updates = states["number_training_updates"]

            if self.use_cuda:
                self.model.cuda()

        elif "autorec" in shared:
            # copy initialized data from shared table
            self.model = shared["autorec"]

        self.criterion = nn.NLLLoss()
        self.optimizer = torch.optim.Adam(
            filter(lambda p: p.requires_grad, self.model.parameters()),
            opt["learningrate"],
        )
        self.metrics = {"loss": 0.0}
        self.metrics["recall@1"] = []
        self.metrics["recall@10"] = []
        self.metrics["recall@50"] = []
        self.metrics["num_tokens"] = 0

    def report(self):
        """
        Report loss and perplexity from model's perspective.

        Note that this includes predicting __END__ and __UNK__ tokens and may
        differ from a truly independent measurement.
        """
        base = super().report()
        m = {}
        m["num_tokens"] = self.metrics["num_tokens"]
        m["loss"] = self.metrics["loss"] / self.metrics["num_tokens"]
        # m["loss"] = self.criterion.normalize_loss_reset(self.metrics["loss"])
        for x in ["1", "10", "50"]:
            if f"recall@{x}" in self.metrics and self.metrics[f"recall@{x}"] != []:
                m[f"recall@{x}"] = np.mean(self.metrics[f"recall@{x}"])
        for k, v in m.items():
            # clean up: rounds to sigfigs and converts tensors to floats
            base[k] = round_sigfigs(v, 4)
        return base

    def reset_metrics(self):
        self.metrics["loss"] = 0.0
        self.metrics["recall@1"] = []
        self.metrics["recall@10"] = []
        self.metrics["recall@50"] = []
        self.metrics["num_tokens"] = 0

    def share(self):
        """Share internal states."""
        shared = super().share()
        shared["autorec"] = self.model
        return shared

    def vectorize(self, obs, history, **kwargs):
        if "text" not in obs:
            return obs
        # TODO: do sentiment analysis when testing or ignore like/dislike
        pattern = re.compile(r"@\d+|#\d+")
        input_match = re.findall(pattern, history.get_history_str())
        input_match = [int(x[1:]) for x in input_match]

        if "labels" in obs:
            label_type = "labels"
        elif "eval_labels" in obs:
            label_type = "eval_labels"
        else:
            label_type = None
        if label_type is None:
            return obs
        pattern = re.compile(r"@\d+")
        labels_match = re.findall(pattern, obs[label_type][0])
        labels_match = [int(x[1:]) for x in labels_match]
        if labels_match == []:
            del obs['text'], obs[label_type]
            return obs

        input_vec = torch.zeros(self.n_movies)
        labels_vec = torch.zeros(self.n_movies, dtype=torch.long)
        input_vec[input_match] = 1
        labels_vec[labels_match] = 1

        obs["text_vec"] = input_vec
        obs[label_type + "_vec"] = labels_vec

        return obs

    def train_step(self, batch):
        bs = (batch.label_vec == 1).sum().item()
        xs = torch.zeros(bs, self.n_movies)
        ys = torch.zeros(bs, dtype=torch.long)
        if self.use_cuda:
            xs = xs.cuda()
            ys = ys.cuda()
        for i, (b, movieIdx) in enumerate(batch.label_vec.nonzero().tolist()):
            xs[i] = batch.text_vec[b]
            ys[i] = movieIdx
        outputs = F.log_softmax(self.model(xs, range01=False), dim=1)
        loss = self.criterion(outputs, ys)

        self.metrics["loss"] += loss.item()
        self.metrics["num_tokens"] += bs

        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()

        self._number_training_updates += 1

    def eval_step(self, batch):
        if batch.text_vec is None:
            return
        bs = (batch.label_vec == 1).sum().item()
        xs = torch.zeros(bs, self.n_movies)
        ys = torch.zeros(bs, dtype=torch.long)
        if self.use_cuda:
            xs = xs.cuda()
            ys = ys.cuda()
        for i, (b, movieIdx) in enumerate(batch.label_vec.nonzero().tolist()):
            xs[i] = batch.text_vec[b]
            ys[i] = movieIdx
        outputs = F.log_softmax(self.model(xs, range01=False), dim=1)
        loss = self.criterion(outputs, ys)

        self.metrics["loss"] += loss.item()
        self.metrics["num_tokens"] += bs
        _, pred_idx = torch.topk(outputs, k=100, dim=1)

        for b in range(bs):
            self.metrics["recall@1"].append(
                int(ys[b].item() in pred_idx[b][:1].tolist())
            )
            self.metrics["recall@10"].append(
                int(ys[b].item() in pred_idx[b][:10].tolist())
            )
            self.metrics["recall@50"].append(
                int(ys[b].item() in pred_idx[b][:50].tolist())
            )