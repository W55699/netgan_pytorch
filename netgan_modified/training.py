from models import Generator, Discriminator
import utils

import numpy as np
import scipy.sparse as sp
from sklearn.metrics import roc_auc_score, average_precision_score
import math

import torch
import torch.optim as optim
from torch.nn.functional import one_hot
from torch.autograd import grad
#from torch.utils.tensorboard import SummaryWriter
import time
from joblib import Parallel, delayed
import pdb
from matplotlib import pyplot as plt

class Trainer():
    def __init__(self, graph, graph_weighted, scaler, N, max_iterations=20000, rw_len=16, batch_size=128, H_gen=40, H_disc=30, H_inp=128, z_dim=16, lr=0.0003, n_critic=3, gp_weight=10.0, betas=(.5, .9),
                 l2_penalty_disc=5e-5, l2_penalty_gen=1e-7, temp_start=5.0, temp_decay=1-5e-5, min_temp=0.5,  val_share=0.1, test_share=0.05, seed=498164, delta=0.02):
        """
            Initialize NetGAN.
            Parameters
            ----------
            graph: scipy_sparse_matrix
                   Graph
            N: int
               Number of nodes in the graph to generate.
            max_iterations: int, default: 40,000
                        Maximal iterations if the stopping_criterion is not fulfilled.
            rw_len: int
                    Length of random walks to generate.
            batch_size: int, default: 128
                        The batch size.
            H_gen: int, default: 40
                   The hidden_size of the generator.
            H_disc: int, default: 30
                    The hidden_size of the discriminator
            H_inp: int, 128
                   Inputsize of the LSTM-Cells
            z_dim: int, 16
                   The dimension of the random noise that is used as input to the generator.
            lr: float, default: 0.0003
                The Learning rate will be used for the generator as well as for the discriminator.
            n_critic: int, default: 3
                      The number of discriminator iterations per generator training iteration.
            gp_weight: float, default: 10
                        Gradient penalty weight for the Wasserstein GAN. See the paper 'Improved Training of Wasserstein GANs' for more details.
            betas: tuple, default: (.5, .9)
                    Decay rates of the Adam Optimizers.
            l2_penalty_gen: float, default: 1e-7
                            L2 penalty on the generator weights.
            l2_penalty_disc: float, default: 5e-5
                             L2 penalty on the discriminator weights.
            temp_start: float, default: 5.0
                        The initial temperature for the Gumbel softmax.
            temp_decay: float, default: 1-5e-5
                        After each evaluation, the current temperature is updated as
                        current_temp := max(temperature_decay*current_temp, min_temperature)
            min_temp: float, default: 0.5
                                      The minimal temperature for the Gumbel softmax.
            val_share: float, default: 0.1
                       Percentage of validation edges.
            test_share: float, default: 0.1
                        Percentage of test edges.
            seed: int, default: 498164
                                Seed for numpy.random. It is used for splitting the graph in train, validation and test sets.


        """
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.max_iterations = max_iterations
        self.rw_len = rw_len
        self.batch_size = batch_size
        self.N = N
        self.state = 'structure'
        self.generator = Generator(H_inputs=H_inp, H=H_gen, N=N, rw_len=rw_len, z_dim=z_dim, temp=temp_start).to(self.device)
        self.generator.freeze_params('structure')
        self.discriminator = Discriminator(H_inputs=H_inp, H=H_disc, N=N, rw_len=rw_len).to(self.device)
        self.G_optimizer = optim.Adam(filter(lambda p: p.requires_grad, self.generator.parameters()), lr=lr, betas=betas)
        self.D_optimizer = optim.Adam(filter(lambda p: p.requires_grad, self.discriminator.parameters()), lr=lr, betas=betas)
        self.n_critic = n_critic
        self.gp_weight = gp_weight
        self.l2_penalty_disc = l2_penalty_disc
        self.l2_penalty_gen =l2_penalty_gen
        self.temp_start = temp_start
        self.temp_decay = temp_decay
        self.min_temp = min_temp

        self.graph = graph
        self.graph_weighted = graph_weighted
        self.scaler = scaler
        self.train_ones, self.val_ones, self.val_zeros, self.test_ones, self.test_zeros = utils.train_val_test_split_adjacency(graph, val_share, test_share, seed, undirected=True, connected=True, asserts=True)
        self.train_graph = sp.coo_matrix((np.ones(len(self.train_ones)), (self.train_ones[:, 0], self.train_ones[:, 1]))).tocsr()
        assert (self.train_graph.toarray() == self.train_graph.toarray().T).all()
        self.walker = utils.RandomWalker(self.train_graph, self.graph_weighted, self.rw_len, p=1, q=1, batch_size=batch_size)
        self.eo = []
        self.critic_loss = []
        self.generator_loss = []
        self.avp = []
        self.roc_auc = []
        self.best_performance = 0.0
        self.running = True
        self.delta = delta
        self.lr = lr
        self.betas = betas
        self.loss_val = []
        self.loss_train = []

    def l2_regularization_G(self, G):
        # regularizaation for the generator. W_down will not be regularized.
        l2_1 = torch.sum(torch.cat([x.view(-1) for x in G.W_down.weight]) ** 2 / 2)
        l2_2 = torch.sum(torch.cat([x.view(-1) for x in G.W_up.weight]) ** 2 / 2)
        l2_3 = torch.sum(torch.cat([x.view(-1) for x in G.W_up.bias]) ** 2 / 2)
        l2_4 = torch.sum(torch.cat([x.view(-1) for x in G.intermediate.weight]) ** 2 / 2)
        l2_5 = torch.sum(torch.cat([x.view(-1) for x in G.intermediate.bias]) ** 2 / 2)
        l2_6 = torch.sum(torch.cat([x.view(-1) for x in G.h_up.weight]) ** 2 / 2)
        l2_7 = torch.sum(torch.cat([x.view(-1) for x in G.h_up.bias]) ** 2 / 2)
        l2_8 = torch.sum(torch.cat([x.view(-1) for x in G.c_up.weight]) ** 2 / 2)
        l2_9 = torch.sum(torch.cat([x.view(-1) for x in G.c_up.bias]) ** 2 / 2)
        l2_10 = torch.sum(torch.cat([x.view(-1) for x in G.lstmcell.cell.weight]) ** 2 / 2)
        l2_11 = torch.sum(torch.cat([x.view(-1) for x in G.lstmcell.cell.bias]) ** 2 / 2)
        l2 = self.l2_penalty_gen * (l2_1 + l2_2 + l2_3 + l2_4 + l2_5 + l2_6 + l2_7 + l2_8 + l2_9 + l2_10 + l2_11)
        return l2

    def l2_regularization_G_lines(self, G):
        # regularizaation for the generator. W_down will not be regularized.
        l2_1 = torch.sum(torch.cat([x.view(-1) for x in G.W_down_lines.weight]) ** 2 / 2)
        l2_2 = torch.sum(torch.cat([x.view(-1) for x in G.h_up_lines.weight]) ** 2 / 2)
        l2_3 = torch.sum(torch.cat([x.view(-1) for x in G.h_up_lines.bias]) ** 2 / 2)
        l2_4 = torch.sum(torch.cat([x.view(-1) for x in G.c_up_lines.weight]) ** 2 / 2)
        l2_5 = torch.sum(torch.cat([x.view(-1) for x in G.c_up_lines.bias]) ** 2 / 2)
        l2_6 = torch.sum(torch.cat([x.view(-1) for x in G.lstmcell_lines.cell.weight]) ** 2 / 2)
        l2_7 = torch.sum(torch.cat([x.view(-1) for x in G.lstmcell_lines.cell.bias]) ** 2 / 2)
        l2_8 = torch.sum(torch.cat([x.view(-1) for x in G.W_out_lines.weight]) ** 2 / 2)
        l2_9 = torch.sum(torch.cat([x.view(-1) for x in G.W_out_lines.bias]) ** 2 / 2)
        l2_10 = torch.sum(torch.cat([x.view(-1) for x in G.intermediate_lines.weight]) ** 2 / 2)
        l2_11 = torch.sum(torch.cat([x.view(-1) for x in G.intermediate_lines.bias]) ** 2 / 2)
        l2 = self.l2_penalty_gen * (l2_1 + l2_2 + l2_3 + l2_4 + l2_5 + l2_6 + l2_7 + l2_8 + l2_9 + l2_10 + l2_11)
        return l2

    def l2_regularization_D(self, D):
        # regularizaation for the discriminator. W_down will not be regularized.
        l2_1 = torch.sum(torch.cat([x.view(-1) for x in D.W_down.weight]) ** 2 / 2)
        l2_2 = torch.sum(torch.cat([x.view(-1) for x in D.lstmcell.cell.weight]) ** 2 / 2)
        l2_3 = torch.sum(torch.cat([x.view(-1) for x in D.lstmcell.cell.bias]) ** 2 / 2)
        l2_4 = torch.sum(torch.cat([x.view(-1) for x in D.lin_out.weight]) ** 2 / 2)
        l2_5 = torch.sum(torch.cat([x.view(-1) for x in D.lin_out.bias]) ** 2 / 2)
        l2 = self.l2_penalty_disc * (l2_1 + l2_2 + l2_3 + l2_4 + l2_5)
        return l2


    #def calc_gp(self, fake_inputs, fake_inputs_weights, real_inputs, real_inputs_weights):
    def calc_gp(self, fake_inputs, real_inputs):
        # calculate the gradient penalty. For more details see the paper 'Improved Training of Wasserstein GANs'.
        alpha = torch.rand((self.batch_size, 1, 1), dtype=torch.float64).to(self.device)
        differences = fake_inputs - real_inputs
        interpolates = real_inputs + alpha * differences
        y_pred_interpolates = self.discriminator(interpolates)
        gradients = grad(outputs=y_pred_interpolates, inputs=interpolates, grad_outputs=torch.ones_like(y_pred_interpolates), create_graph=True, retain_graph=True)[0]
        slopes = torch.sqrt(torch.sum(gradients ** 2, dim=[1, 2]))
        gradient_penalty = torch.mean((slopes - 1) ** 2)
        gradient_penalty = gradient_penalty * self.gp_weight
        return gradient_penalty

    def critic_train_iteration(self):
        self.D_optimizer.zero_grad()
        # create fake and real inputs
        fake_inputs_rw, fake_inputs_weights = self.generator.sample(self.batch_size, self.device)
        random_walks, weights = self.walker.walk()

        real_inputs = one_hot(torch.tensor(random_walks, dtype=torch.int64), num_classes=self.N).type(torch.float64).to(self.device)
        real_inputs_weights = torch.tensor(weights, dtype=torch.float64).to(self.device)
        if(self.state=='structure'):
            real_inputs_weights = torch.zeros_like(real_inputs_weights)
            fake_inputs_weights = torch.zeros_like(fake_inputs_weights)
        real_inputs = torch.cat((real_inputs, real_inputs_weights), dim=2)
        fake_inputs = torch.cat((fake_inputs_rw, fake_inputs_weights), dim=2)

        y_pred_fake = self.discriminator(fake_inputs)
        y_pred_real = self.discriminator(real_inputs)

        gp = self.calc_gp(fake_inputs, real_inputs)  # gradient penalty
        disc_cost = torch.mean(y_pred_fake) - torch.mean(y_pred_real) + gp + self.l2_regularization_D(self.discriminator)

        disc_cost.backward()
        self.D_optimizer.step()
        return disc_cost.item()

    def generator_train_iteration(self):
        self.generator.train()
        self.G_optimizer.zero_grad()
        fake_inputs_rw, fake_weights = self.generator.sample(self.batch_size, self.device)
        if(self.state=='structure'): fake_weights = torch.zeros_like(fake_weights)
        fake_inputs = torch.cat((fake_inputs_rw, fake_weights), dim=2)
        y_pred_fake = self.discriminator(fake_inputs)

        if(self.state=='structure'): gen_cost = -torch.mean(y_pred_fake) + self.l2_regularization_G(self.generator)
        else: gen_cost = -torch.mean(y_pred_fake) + self.l2_regularization_G_lines(self.generator)
        gen_cost.backward()
        self.G_optimizer.step()
        return gen_cost.item()

    def create_graph(self, num_samples, i, reset_weights=False):
        if reset_weights:
            self.generator.reset_weights()
        self.generator.eval()

        self.generator.temp = 0.5
        samples, samples_lines = [], []
        num_iterations = int(num_samples/1000)+1
        print('create graph:')
        for j in range(num_iterations):
            if(j%10 == 1): print('{}/{}'.format(j, num_iterations))
            rw_smpls, lines_smpls = self.generator.sample_discrete(int(num_samples / 1000), self.device)
            samples.append(rw_smpls)
            samples_lines.append(lines_smpls)
        samples = np.vstack(samples)
        gr, gr_weights = utils.score_matrix_from_random_walks(samples, self.N, samples_lines)
        gr = gr.tocsr()

        # Assemble a graph from the score matrix
        _graph = utils.graph_from_scores(gr, self.graph.sum())

        # Compute edge overlap
        edge_overlap = utils.edge_overlap(self.graph.toarray(), _graph)
        edge_scores = np.append(gr[tuple(self.val_ones.T)].A1, gr[tuple(self.val_zeros.T)].A1)
        actual_labels_val = np.append(np.ones(len(self.val_ones)), np.zeros(len(self.val_zeros)))
        # Compute Validation ROC-AUC and average precision scores.
        self.roc_auc.append(roc_auc_score(actual_labels_val, edge_scores))
        self.avp.append(average_precision_score(actual_labels_val, edge_scores))
        self.eo.append(edge_overlap/self.graph.sum())

        loss_lines = utils.calc_lines_mse(self.train_ones, self.graph_weighted, gr_weights)
        loss_lines_val = utils.calc_lines_mse(self.val_ones, self.graph_weighted, gr_weights)
        print('roc: {:.4f}   avp: {:.4f}   eo: {:.4f}'.format(self.roc_auc[-1], self.avp[-1], self.eo[-1]))
        print('loss_lines Train: {}'.format(loss_lines))
        print('loss_lines Val: {}'.format(loss_lines_val))
        self.loss_train.append(loss_lines)
        self.loss_val.append(loss_lines_val)
        self.generator.temp = np.maximum(self.temp_start * np.exp(-(1 - self.temp_decay) * i), self.min_temp)


    def check_running(self, i):
        torch.save(self, str(i+1)+ '_model.pt')
        if(self.state=='structure'):
            if (self.stopping_criterion == 'val'):
                if (self.roc_auc[-1] + self.avp[-1] > self.best_performance + self.delta):
                    self.best_performance = self.roc_auc[-1] + self.avp[-1]
                    self.patience = self.max_patience
                else:
                    self.patience -= 1

                if self.patience == 0:
                    print('Strucutre is finished after {} iterations, start training line length'.format(i))
                    self.state = 'lines'
                    self.generator.freeze_params('lines')
                    self.G_optimizer = optim.Adam(filter(lambda p: p.requires_grad, self.generator.parameters()), lr=self.lr, betas=self.betas)

            else:
                if (self.stopping_eo < self.eo[-1]):
                    print('Strucutre is finished after {} iterations, start training line length'.format(i))
                    self.state = 'lines'
                    self.generator.freeze_params('lines')
                    self.G_optimizer = optim.Adam(filter(lambda p: p.requires_grad, self.generator.parameters()), lr=self.lr, betas=self.betas)

    def initialize_validation_settings(self, stopping_criterion, stopping_eo, max_patience):
        self.stopping_criterion = stopping_criterion
        self.stopping_eo = stopping_eo  # needed for 'eo' criterion         #
        self.max_patience = max_patience  # needed for 'val' criterion
        self.patience = max_patience  #
        if (self.stopping_criterion == 'val'):
            print("**** Using VAL criterion for early stopping with max patience of: {}****".format(self.max_patience))
        else:
            assert self.stopping_eo is not None, "stopping_eo is not a float"
            print("**** Using EO criterion of {} for early stopping".format(self.stopping_eo))

    def plot_graph(self):
        if len(self.critic_loss) > 10:
            plt.plot(self.critic_loss[9::], label="Critic loss")
            plt.plot(self.generator_loss[9::], label="Generator loss")
        else:
            plt.plot(self.critic_loss, label="Critic loss")
            plt.plot(self.generator_loss, label="Generator loss")
        plt.legend()
        plt.show()

    def train(self, create_graph_every = 2000, plot_graph_every=500, num_samples_graph = 100000, stopping_criterion='val', max_patience=5, stopping_eo=None, i_start=0):
        """
        create_graph_every: int, default: 2000
                            Creates every nth iteration a graph from randomwalks.
        plot_graph_every: int, default: 2000
                         Plots the lost functions of the generator and discriminator.
        num_samples_graph: int, default 10000
                            Number of random walks that will be created for the graphs. Higher values mean more precise evaluations but also more computational time.
        stopping_criterion: str, default: 'val'
                            The stopping_criterion can be either 'val' or 'eo':
                            'val': Stops the optimization if there are no improvements after several iterations. --> defined by max_patience
                            'eo': Stops if the edge overlap exceeds a certain treshold. --> defined by stopping_eo
        max_patience: int, default: 5
                      Maximum evaluation steps without improvement of the validation accuracy to tolerate. Only
                      applies to the VAL criterion.
        stopping_eo: float in (0,1], default: 0.5
                     Stops when the edge overlap exceeds this threshold. Will be used when stopping_criterion is 'eo'.
        """
        self.initialize_validation_settings(stopping_criterion, stopping_eo, max_patience)
        starting_time = time.time()
        # Start Training
        for i in np.arange(i_start, self.max_iterations):
            if(self.running):
                self.critic_loss.append(np.mean([self.critic_train_iteration() for _ in range(self.n_critic)]))
                self.generator_loss.append(self.generator_train_iteration())
                if (i % create_graph_every == create_graph_every-1):
                    self.create_graph(num_samples_graph,  i)
                    self.check_running(i)
                    print('Took {} minutes so far..'.format((time.time() - starting_time)/60))
                if plot_graph_every > 0 and (i + 1) % plot_graph_every == 0: self.plot_graph()
