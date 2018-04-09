import torch
import torch.nn as nn
from torch.optim.lr_scheduler import *
import numpy as np
from .base import Learner
from .aggregator import MultistepAggregatorWithInfo 
from surreal.model.ppo_net import PPOModel, DiagGauss
from surreal.session import Config, extend_config, BASE_SESSION_CONFIG, BASE_LEARNER_CONFIG, ConfigError
from surreal.utils.pytorch import GpuVariable as Variable
import surreal.utils as U
from surreal.utils.pytorch.hyper_scheduler import *   

class PPOLearner(Learner):
    '''
    PPOLearner: subclass of Learner that contains PPO algorithm logic
    Attributes:
        gpu_id: array of gpu IDs. None if using CPU
        model: instance of PPOModel from surreal.model.ppo_net
        ref_target_model: instance of PPOModel, kept to used as
            reference policy
        ppo_mode: string of either 'adapt' or 'clip' to determine
            which variant of PPO is used. For details of variants
            see https://arxiv.org/pdf/1707.06347.pdf
        norm_adv: boolean flag -- whether to use batch advantage
            normalization
        use_z_filter: boolean flag -- whether to use obs Z-Filtering
        actor/critic_optim: Adam Optimizer for policy and baseline network
        actor/critic_lr_scheduler: Learning rate scheduler. details see
            surreal.utils.pytorch.scheduler
        aggregator: experience aggregator used to batch experiences.
            for available aggregators, see surreal.learner.aggregator
        pd: probability distribution class (Assumed as Diagonal Gaussian)
            see surreal.model.ppo_net for details

    important member functions:
        private methods:
        _clip_loss: computes the loss and various statistics
            for 'clip' variant PPO
        _clip_update: uses loss information to make policy update
        _adapt_loss: computes loss and various statistics for
            'adapt' variant of PPO
        _adapt_update: uses loss info to make policy update
        _value_loss: computes loss and various statistics for value function
        _value_update: uses loss info to update value function
        _gae_and_return: computes generalized advantage estimate and
            corresponding N-step return. Details of algorithm can be found
            here: https://arxiv.org/pdf/1506.02438.pdf
        _advantage_and_return: basic advantage and N-step return estimate
        _optimize: fucntion that makes policy and value function update
        _post_publish: function that manages metrics and behavior after
            parameter release

        public methods:
        learn: method to perform optimization and send to tensorplex for log
        module_dict: returns the corresponding parameters
        publish_parameter: publishes parameters in self.model to parameter server
    '''
    def __init__(self, learner_config, env_config, session_config):
        super().__init__(learner_config, env_config, session_config)

        # GPU setting
        self.global_step = 0
        num_gpus = session_config.learner.num_gpus
        self.gpu_id = list(range(num_gpus))
        self.use_cuda = len(self.gpu_id) > 0

        if not self.use_cuda:
            self.log.info('Using CPU')
        else:
            self.log.info('Using GPU: {}'.format(self.gpu_id))

        # RL general parameters
        self.gamma = self.learner_config.algo.gamma
        self.lam   = self.learner_config.algo.lam
        self.n_step = self.learner_config.algo.n_step
        self.use_z_filter = self.learner_config.algo.use_z_filter
        self.norm_adv = self.learner_config.algo.norm_adv
        self.batch_size = self.learner_config.algo.batch_size

        self.action_dim = self.env_config.action_spec.dim[0]
        self.obs_dim = self.env_config.obs_spec.dim[0]
        self.init_log_sig = self.learner_config.algo.consts.init_log_sig
        self.model = PPOModel(
            init_log_sig=self.init_log_sig,
            obs_dim=self.obs_dim,
            action_dim=self.action_dim,
            use_z_filter=self.use_z_filter,
            rnn_config = self.learner_config.algo.rnn,
            use_cuda = self.use_cuda, 
        )
        self.ref_target_model = PPOModel(
            init_log_sig=self.init_log_sig,
            obs_dim=self.obs_dim,
            action_dim=self.action_dim,
            use_z_filter=self.use_z_filter,
            rnn_config = self.learner_config.algo.rnn,
            use_cuda = self.use_cuda, 
        )
        self.ref_target_model.update_target_params(self.model)

        # PPO parameters
        self.ppo_mode = self.learner_config.algo.ppo_mode
        self.if_rnn_policy = self.learner_config.algo.rnn.if_rnn_policy
        self.is_weight_thresh = self.learner_config.algo.consts.is_weight_thresh
        self.lr_policy = self.learner_config.algo.lr.lr_policy
        self.lr_baseline = self.learner_config.algo.lr.lr_baseline
        self.epoch_policy = self.learner_config.algo.consts.epoch_policy
        self.epoch_baseline = self.learner_config.algo.consts.epoch_baseline
        self.kl_target = self.learner_config.algo.consts.kl_target
        self.adjust_threshold = self.learner_config.algo.consts.adjust_threshold

        # PPO mode 'adjust'
        self.kl_cutoff_coeff = self.learner_config.algo.adapt_consts.kl_cutoff_coeff
        self.beta_init = self.learner_config.algo.adapt_consts.beta_init
        self.beta_range = self.learner_config.algo.adapt_consts.beta_range

        # PPO mode 'clip'
        self.clip_range = self.learner_config.algo.clip_consts.clip_range
        self.clip_epsilon_init = self.learner_config.algo.clip_consts.clip_epsilon_init

        if self.ppo_mode == 'adapt':
            self.beta = self.beta_init
            self.eta = self.kl_cutoff_coeff
            self.beta_upper = self.beta_range[1]
            self.beta_lower = self.beta_range[0]
            self.beta_adjust_threshold = self.adjust_threshold
        else: # method == 'clip'
            self.clip_epsilon = self.clip_epsilon_init
            self.clip_adjust_threshold = self.adjust_threshold
            self.clip_upper = self.clip_range[1]
            self.clip_lower = self.clip_range[0]

        self.exp_counter = 0
        self.kl_record = []

        with U.torch_gpu_scope(self.gpu_id):

            # Learning parameters and optimizer
            self.clip_actor_gradient = self.learner_config.algo.gradient.clip_actor
            self.actor_gradient_clip_value = self.learner_config.algo.gradient.clip_actor_val
            self.clip_critic_gradient = self.learner_config.algo.gradient.clip_critic
            self.critic_gradient_clip_value = self.learner_config.algo.gradient.clip_critic_val
            self.critic_optim = torch.optim.Adam(
                self.model.critic.parameters(),
                lr=self.lr_baseline
            )
            self.actor_optim = torch.optim.Adam(
                self.model.actor.parameters(),
                lr=self.lr_policy
            )

            # learning rate scheduler
            self.min_lr = self.learner_config.algo.lr.min_lr
            self.lr_update_frequency = self.learner_config.algo.lr.lr_update_frequency
            self.frames_to_anneal = self.learner_config.algo.lr.frames_to_anneal
            num_updates = int(self.frames_to_anneal / (self.learner_config.replay.param_release_min *
                                                       self.learner_config.algo.stride))
            scheduler = eval(self.learner_config.algo.lr.lr_scheduler) 
            self.actor_lr_scheduler  = scheduler(self.actor_optim, 
                                                 num_updates,
                                                 update_freq=self.lr_update_frequency,
                                                 min_lr = self.min_lr)
            self.critic_lr_scheduler = scheduler(self.critic_optim, 
                                                 num_updates,
                                                 update_freq=self.lr_update_frequency,
                                                 min_lr = self.min_lr)

            # Experience Aggregator
            self.aggregator = MultistepAggregatorWithInfo(self.env_config.obs_spec, 
                                                          self.env_config.action_spec)
        
            # probability distribution. Gaussian only for now
            self.pd = DiagGauss(self.action_dim)

            # placeholder for RNN hidden cells
            self.cells = None

    def _clip_loss(self, obs, actions, advantages, behave_pol): 
        """
        Computes the loss with current data. also returns a dictionary of statistics
        which includes surrogate loss, clipped surrogate los, policy entropy, clip
        constant
        return: surreal.utils.pytorch.GPUVariable, dict
        Args:
            obs: batch of observations in form of (batch_size, obs_dim)
            actions: batch of actions in form of (batch_size, act_dim)
            advantages: batch of normalized advantage, (batch_size, 1)
            behave_pol: batch of behavior policy (batch_size, 2 * act_dim)
        Returns:
            clip_loss: Variable for loss
            stats: dictionary of recorded statistics
        """
        learn_pol = self.model.forward_actor(obs)
        learn_prob = self.pd.likelihood(actions, learn_pol)
        behave_prob = self.pd.likelihood(actions, behave_pol)
        prob_ratio = learn_prob / behave_prob
        cliped_ratio = torch.clamp(prob_ratio, 1 - self.clip_epsilon, 
                                               1 + self.clip_epsilon)
        surr = -prob_ratio * advantages
        cliped_surr = -cliped_ratio * advantages
        clip_loss = torch.cat([surr, cliped_surr], 1).max(1)[0].mean()

        stats = {
            "_surr_loss": surr.mean().data[0], 
            "_clip_surr_loss": clip_loss.data[0], 
            "_entropy": self.pd.entropy(learn_pol).data.mean(),
            '_clip_epsilon': self.clip_epsilon
        }
        return clip_loss, stats

    def _clip_update(self, obs, actions, advantages, behave_pol):
        """
        Method that makes policy updates. calls _clip_loss method
        Note:  self.clip_actor_gradient determines whether gradient is clipped
        return: dictionary of statistics to be sent to tensorplex server
        Args:
            obs: batch of observations in form of (batch_size, obs_dim)
            actions: batch of actions in form of (batch_size, act_dim)
            advantages: batch of normalized advantage, (batch_size, 1)
            behave_pol: batch of behavior policy (batch_size, 2 * act_dim)
        Returns:
            stats: dictionary of recorded statistics
        """
        loss, stats = self._clip_loss(obs, actions, advantages, behave_pol)
        self.model.actor.zero_grad()
        loss.backward()
        if self.clip_actor_gradient:
            nn.utils.clip_grad_norm(self.model.actor.parameters(), 
                                    self.actor_gradient_clip_value)
        self.actor_optim.step()
        return stats

    def _adapt_loss(self, obs, actions, advantages, behave_pol, ref_pol):
        """
        Computes the loss with current data. also returns a dictionary of statistics
        which includes surrogate loss, clipped surrogate los, policy entropy, adaptive
        KL penalty constant, policy KL divergence
        return: surreal.utils.pytorch.GPUVariable, dict
        Args:
            obs: batch of observations in form of (batch_size, obs_dim)
            actions: batch of actions in form of (batch_size, act_dim)
            advantages: batch of normalized advantage, (batch_size, 1)
            behave_pol: batch of behavior policy (batch_size, 2 * act_dim)
            ref_pol: batch of reference policy (batch_size, 2 * act_dim)
        Returns:
            loss: Variable for loss
            stats: dictionary of recorded statistics
        """
        learn_pol = self.model.forward_actor(obs, self.cells)
        prob_behave = self.pd.likelihood(actions, behave_pol)
        prob_learn  = self.pd.likelihood(actions, learn_pol)
        
        kl = self.pd.kl(ref_pol, learn_pol).mean()
        surr = -(advantages.view(-1, 1) * torch.clamp(prob_learn/prob_behave, 
                                            max=self.is_weight_thresh)).mean()
        loss = surr + self.beta * kl
        entropy = self.pd.entropy(learn_pol).mean()

        if kl.data[0] - 2.0 * self.kl_target > 0:
            loss += self.eta * (kl - 2.0 * self.kl_target).pow(2)

        stats = {
            '_kl_loss_adapt': loss.data[0], 
            '_surr_loss': surr.data[0], 
            '_pol_kl': kl.data[0], 
            '_entropy': entropy.data[0],
            '_beta': self.beta
        }
        return loss, stats

    def _adapt_update(self, obs, actions, advantages, behave_pol, ref_pol):
        """
        Method that makes policy updates. calls _adapt_loss method
        Note:  self.clip_actor_gradient determines whether gradient is clipped
        return: dictionary of statistics to be sent to tensorplex server
        Args:
            obs: batch of observations in form of (batch_size, obs_dim)
            actions: batch of actions in form of (batch_size, act_dim)
            advantages: batch of normalized advantage, (batch_size, 1)
            behave_pol: batch of behavior policy (batch_size, 2 * act_dim)
            ref_pol: batch of reference policy (batch_size, 2 * act_dim)
        Returns:
            stats: dictionary of recorded statistics
        """
        loss, stats = self._adapt_loss(obs, actions, advantages, behave_pol, ref_pol)
        self.model.actor.zero_grad()
        loss.backward()
        if self.clip_actor_gradient:
            nn.utils.clip_grad_norm(self.model.actor.parameters(), 
                                    self.actor_gradient_clip_value)
        self.actor_optim.step()
        return stats

    def _value_loss(self, obs, returns):
        """
        Computes the loss with current data. also returns a dictionary of statistics
        which includes value loss and explained variance
        return: surreal.utils.pytorch.GPUVariable, dict
        Args:
            obs: batch of observations in form of (batch_size, obs_dim)
            returns: batch of N-step return estimate (batch_size,)
        Returns:
            loss: Variable for loss
            stats: dictionary of recorded statistics
        """
        values = self.model.forward_critic(obs, self.cells)
        if len(values.size()) == 3: values = values.squeeze(2)
        explained_var = 1 - torch.var(returns - values) / torch.var(returns)
        loss = (values - returns).pow(2).mean()

        stats = {
            '_val_loss': loss.data[0],
            '_val_explained_var': explained_var.data[0]
        }
        return loss, stats

    def _value_update(self, obs, returns):
        """
        Method that makes baseline function updates. calls _value_loss method
        Note:  self.clip_actor_gradient determines whether gradient is clipped
        return: dictionary of statistics to be sent to tensorplex server
        Args:
            obs: batch of observations in form of (batch_size, obs_dim)
            returns: batch of N-step return estimate (batch_size,)
        Returns:
            stats: dictionary of recorded statistics
        """
        loss, stats = self._value_loss(obs, returns)
        self.model.critic.zero_grad()
        loss.backward()
        if self.clip_critic_gradient:
            nn.utils.clip_grad_norm(self.model.critic.parameters(), 
                                    self.critic_gradient_clip_value)
        self.critic_optim.step()

        return stats

    def _gae_and_return(self, obs, obs_next, actions, rewards, dones):
        '''
        computes generalized advantage estimate and corresponding N-step return. 
        Details of algorithm can be found here: https://arxiv.org/pdf/1506.02438.pdf
        Args: 
            obs: batch of observations (batch_size, N-step , obs_dim)
            obs_next: batch of next observations (batch_size, 1 , obs_dim)
            actions: batch of actions (batch_size, N-step , act_dim)
            rewards: batch of rewards (batch_size, N-step)
            dones: batch of termination flags (batch_size, N-step)
        Returns:
            obs: batch of observation (batch_size, obs_dim)
            actions: batch of action (batch_size, act_dim)
            advantage: batch of advantages (batch_size, 1)
            returns: batch of returns (batch_size, 1)
        '''
        with U.torch_gpu_scope(self.gpu_id):
            gamma = torch.pow(self.gamma, U.to_float_tensor(range(self.n_step)))
            lam = torch.pow(self.lam, U.to_float_tensor(range(self.n_step)))
            if self.use_cuda: 
                rewards = rewards.cuda()
                dones = dones.cuda()
                gamma = gamma.cuda()
                lam   = lam.cuda()

            obs_concat = torch.cat([obs, obs_next], dim=1).view(self.batch_size * (self.n_step + 1), -1)
            values = self.model.forward_critic(Variable(obs_concat), self.cells) # (batch, n+1, act_dim), (batch, n+1, 1)
            values = values.view(self.batch_size, self.n_step + 1).data    
            values[:, 1:] *= 1 - dones

            if self.if_rnn_policy:
                # compute return and advantage then normalize
                # to be implemented
                pass
            else:
                returns = torch.sum(gamma * rewards, 1) + values[:, -1] * (self.gamma ** self.n_step)
                tds = rewards + self.gamma * values[:, 1:] - values[:, :-1] 
                gae = torch.sum(tds * gamma * lam, 1)

                if self.norm_adv:
                    std = gae.std()
                    mean = gae.mean()
                    gae = (gae - mean) / std

                return obs[:, 0, :], actions[:, 0, :], gae.view(-1, 1), returns.view(-1, 1)

    def _advantage_and_return(self, obs, obs_next, actions, rewards, dones):
        '''
        computes  advantage estimate and corresponding N-step return with following:
            A = R - V(s)
        Args: 
            obs: batch of observations (batch_size, N-step , obs_dim)
            obs_next: batch of next observations (batch_size, 1 , obs_dim)
            actions: batch of actions (batch_size, N-step , act_dim)
            rewards: batch of rewards (batch_size, N-step)
            dones: batch of termination flags (batch_size, N-step)
        Returns:
            obs: batch of observation (batch_size, obs_dim)
            actions: batch of action (batch_size, act_dim)
            advantage: batch of advantages (batch_size, 1)
            returns: batch of returns (batch_size, 1)
        '''
        with U.torch_gpu_scope(self.gpu_id):
            gamma = torch.pow(self.gamma, U.to_float_tensor(range(self.n_step)))
            returns = torch.zeros(self.batch_size, self.n_step)

            if self.use_cuda: 
                rewards = rewards.cuda()
                dones = dones.cuda()
                gamma = gamma.cuda()
                returns = returns.cuda()

            obs_concat = torch.cat([obs, obs_next], dim=1)
            if not self.if_rnn_policy:
                obs_concat = obs_concat.view(self.batch_size * (self.n_step + 1), -1)
            # obs_concat = obs_concat.contiguous()

            values = self.model.forward_critic(Variable(obs_concat), self.cells) # (batch, n+1, act_dim), (batch, n+1, 1)
            values = values.view(self.batch_size, self.n_step + 1).data    
            values[:, 1:] *= 1 - dones

            if self.if_rnn_policy:
                for step in range(self.n_step):
                    returns[:, step] = torch.sum(gamma[step:] * rewards[:, step:], 1) + \
                                       values[:, -1] * (self.gamma ** (self.n_step - step))
                advs = returns - values[:, :-1] # (batch, n_step)
                if self.norm_adv:
                    std = advs.std()
                    mean = advs.mean()
                    advs = (advs - mean) / std
                return obs, actions, advs, returns
            else:
                returns = torch.sum(gamma * rewards, 1) + values[:, -1] * (self.gamma ** self.n_step)
                advs    = returns - values[:, 0]
                if self.norm_adv:
                    std = advs.std()
                    mean = advs.mean()
                    advs = (advs - mean) / std
                return obs[:, 0, :], actions[:, 0, :], advs.view(-1, 1), returns.view(-1, 1)

    def _optimize(self, obs, actions, rewards, obs_next, persistent_infos, one_time_infos, dones):
        '''
            main method for optimization that calls _adapt/clip_update and 
            _value_update epoch_policy and epoch_baseline times respectively
            return: dictionary of tracted statistics
            Args:
                obs: batch of observations (batch_size, N-step , obs_dim)
                obs_next: batch of next observations (batch_size, 1 , obs_dim)
                actions: batch of actions (batch_size, N-step , act_dim)
                rewards: batch of rewards (batch_size, N-step)
                dones: batch of termination flags (batch_size, N-step)
                action_infos: list of batched other attributes tracted, such as
                    behavior policy, RNN hidden states and etc.
            Returns:
                dictionary of recorded statistics
        '''
        with U.torch_gpu_scope(self.gpu_id):
                test_param_tensor = Variable(torch.FloatTensor([0.1315, 0.5584, 0.4560, 0.2635, 0.2779, 0.3260,0.1315, 0.5584, 0.4560, 0.2635, 0.2779, 0.3260,0.1315, 0.5584, 0.4560, 0.2635, 0.2779])).view(1, 1, 17) 
                print(self.ref_target_model.forward_actor(test_param_tensor))
                pds = persistent_infos[-1]

                if self.if_rnn_policy:
                    h = Variable(one_time_infos[0].transpose(0, 1))
                    c = Variable(one_time_infos[1].transpose(0, 1))
                    self.cells = (h, c)

                advantage_return = self._advantage_and_return(obs, 
                                                        obs_next, 
                                                        actions, 
                                                        rewards, 
                                                        dones)
                obs_iter = Variable(advantage_return[0])
                actions_iter = Variable(advantage_return[1])
                advantages = Variable(advantage_return[2])
                returns = Variable(advantage_return[3])
                behave_pol = Variable(pds[:, 0, :])
                ref_pol = self.ref_target_model.forward_actor(obs_iter, self.cells).detach()

                if self.if_rnn_policy:
                    h = Variable(self.cells[0].data)
                    c = Variable(self.cells[1].data)
                    self.cells = (h, c)
                    behave_pol = Variable(pds)

                for _ in range(self.epoch_policy):
                    if self.ppo_mode == 'clip':
                        stats =  self._clip_update(obs_iter, 
                                                   actions_iter, 
                                                   advantages, 
                                                   behave_pol)
                    else: 
                        stats = self._adapt_update(obs_iter, 
                                                   actions_iter, 
                                                   advantages, 
                                                   behave_pol, 
                                                   ref_pol)
                    curr_pol = self.model.forward_actor(obs_iter).detach()
                    kl = self.pd.kl(ref_pol, curr_pol).mean()
                    stats['_pol_kl'] = kl.data[0]
                    if kl.data[0] > self.kl_target * 4: break
                self.kl_record.append(stats['_pol_kl'])

                for _ in range(self.epoch_baseline):
                    baseline_stats = self._value_update(obs_iter, returns)

                # updating tensorplex
                for k in baseline_stats:
                    stats[k] = baseline_stats[k]

                new_pol = self.model.forward_actor(obs_iter).detach()
                new_likelihood = self.pd.likelihood(actions_iter, new_pol)
                ref_likelihood = self.pd.likelihood(actions_iter, ref_pol)
                behave_likelihood = self.pd.likelihood(actions_iter, behave_pol)

                stats['_avg_return_targ'] = returns.mean().data[0]
                stats['_avg_log_sig'] = self.model.actor.log_var.mean().data[0]
                stats['_avg_behave_likelihood'] = behave_likelihood.mean().data[0]
                stats['_ref_behave_diff'] = self.pd.kl(ref_pol, behave_pol).mean().data[0]
                stats['_avg_IS_weight'] = (new_likelihood/torch.clamp(behave_likelihood, min = 1e-5)).mean().data[0]
                stats['_lr'] = self.actor_lr_scheduler.get_lr()[0]
                
                if self.use_z_filter:
                    self.model.z_update(obs_iter)
                    stats['obs_running_mean'] = np.mean(self.model.z_filter.running_mean())
                    stats['obs_running_square'] =  np.mean(self.model.z_filter.running_square())
                    stats['obs_running_std'] = np.mean(self.model.z_filter.running_std())
                    # self.ref_target_model.update_target_z_filter(self.model)
        return stats

    def learn(self, batch):
        '''
            main method for learning, calls _optimize. Also sends update stats 
            to Tensorplex
            Args:
                batch: pre-aggregated list of experiences rolled out by the agent
        '''
        batch = self.aggregator.aggregate(batch)
        tensorplex_update_dict = self._optimize(
            batch.obs,
            batch.actions,
            batch.rewards,
            batch.next_obs,
            batch.persistent_infos,
            batch.onetime_infos, 
            batch.dones,
        )
        self.tensorplex.add_scalars(tensorplex_update_dict, self.global_step)
        self.exp_counter += self.batch_size
        self.global_step += 1

    def module_dict(self):
        '''
        returns the corresponding parameters
        '''
        return {
            'ppo': self.model,
        }

    def publish_parameter(self, iteration, message=''):
        """
        Learner publishes latest parameters to the parameter server only when accumulated
            enough experiences specified by learner_config.replay.param_release_min
        Note: this overrides the base class publish_parameter method
        Args:
            iteration: the current number of learning iterations
            message: optional message, must be pickleable.
        """
        if self.exp_counter >= self.learner_config.replay.param_release_min:
            self._ps_publisher.publish(iteration, message=message)
            self._post_publish()  

    def _post_publish(self):
        '''
            function that manages metrics and behavior after parameter release
            Actions include: 
                adjusts adaptive threshold for KL penalty for 'adapt' PPO 
                adjusts adaptive prob ratio clip rate for 'clip' PPO
                clears KL-Divergence record
                clears experience counter after parameter release
                steps actor and critic learning rate scheduler
        '''
        final_kl = np.mean(self.kl_record)
        if self.ppo_mode == 'clip': # adapts clip ratios
            if final_kl > self.kl_target * self.clip_adjust_threshold[1]:
                if self.clip_lower < self.clip_epsilon:
                    self.clip_epsilon = self.clip_epsilon / self.learner_config.algo.clip_consts.scale_constant
            elif final_kl < self.kl_target * self.clip_adjust_threshold[0]:
                if self.clip_upper > self.clip_epsilon:
                    self.clip_epsilon = self.clip_epsilon * self.learner_config.algo.clip_consts.scale_constant
        else: # adapt KL divergence penalty before returning the statistics 
            if final_kl > self.kl_target * self.beta_adjust_threshold[1]:
                if self.beta_upper > self.beta:
                    self.beta = self.beta * self.learner_config.algo.adapt_consts.scale_constant
            elif final_kl < self.kl_target * self.beta_adjust_threshold[0]:
                if self.beta_lower < self.beta:
                    self.beta = self.beta / self.learner_config.algo.adapt_consts.scale_constant
        self.ref_target_model.update_target_params(self.model)
        self.kl_record = []
        self.exp_counter = 0
        self.actor_lr_scheduler.step()
        self.critic_lr_scheduler.step()

'''
Current problem:
    1) ref-behave-diff blowing up even with reference model
        potential explanation:
        * Z-filter updated in reference model but not in agent
        * if step pull: end up having to interrupt in the middle of stride 10
'''
# prob because batched n-step rollout is wrong
#  