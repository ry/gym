from skimage.io import imread  # for some reason this needs to be loaded before tf

import sys
import os
import errno
import gym
import time
import skimage.io
import tensorflow as tf
import numpy as np
import random

import resnet as resnet
from replay_memory import ReplayMemory

from gym.envs.attention.common import *

DEBUG = False
DQN_GAMMA = 0.99
MOVING_AVERAGE_DECAY = 0.9
BATCH_SIZE = 1  # TODO allow training on batches

FLAGS = tf.app.flags.FLAGS
tf.app.flags.DEFINE_boolean('resume', False, 'resume from latest saved state')
tf.app.flags.DEFINE_boolean('var_histograms', False,
                            'histogram summaries of every variable')
tf.app.flags.DEFINE_boolean('grad_histograms', False,
                            'histogram summaries of every gradient')
tf.app.flags.DEFINE_boolean('show_train_window', False,
                            'show the training window')
tf.app.flags.DEFINE_float('learning_rate', 0.01, 'Initial learning rate.')
tf.app.flags.DEFINE_integer('num_episodes', 100000,
                            'number of epsidoes to run')
tf.app.flags.DEFINE_integer('glimpse_size', 32, '32 or 64')
tf.app.flags.DEFINE_integer('hidden_size', 64, '')
tf.app.flags.DEFINE_integer('batch_size', 16, '')
tf.app.flags.DEFINE_integer('max_episode_steps', 10, '')
tf.app.flags.DEFINE_string('train_dir', '/tmp/agent_train',
                           """Directory where to write event logs """
                           """and checkpoint.""")
tf.app.flags.DEFINE_string('restore_resnet', '',
                           'path to resnet ckpt to restore from')


def log(msg):
    if DEBUG: print msg


class Episode:
    def __init__(self, first_observation_params):
        self.states = []
        self.actions = []
        self.rewards = []
        self.step = 0
        self._done = False
        self._obvs_set = False
        self.img_fn = first_observation_params[0]
        self.y_params = [first_observation_params[1]]
        self.x_params = [first_observation_params[2]]
        self.zoom_params = [first_observation_params[3]]

    @property
    def obvs(self):
        assert self._done
        if self._obvs_set:
            return self._obvs

        # TODO need data_dir
        data_dir = "/Users/ryan/data/imagenet-small/imagenet-small-train"

        full_img_fn = os.path.join(data_dir, self.img_fn)
        img = imread(full_img_fn)
        if len(img.shape) == 2:
            img = np.dstack([img, img, img])

        self._obvs = np.zeros((self.num_frames, FLAGS.glimpse_size,
                               FLAGS.glimpse_size, 3))
        for i in range(self.num_frames):
            frame = make_observation(img=img,
                                     glimpse_size=FLAGS.glimpse_size,
                                     y=self.y_params[i],
                                     x=self.x_params[i],
                                     zoom=self.zoom_params[i])
            self._obvs[i, :] = frame

        self._obvs_set = True
        return self._obvs

    @property
    def num_frames(self):
        assert self._done
        return len(self.y_params)

    @property
    def num_actions(self):
        assert self._done
        return self.step

    def done(self):
        self._done = True

    def get_state(self, i):
        if i == 0:
            return np.zeros((FLAGS.hidden_size))
        else:
            return self.states[i - 1]

    def store(self, observation_params, action, reward, state):
        assert not self._done
        assert observation_params[0] == self.img_fn
        self.actions.append(action)
        self.rewards.append(reward)
        self.y_params.append(observation_params[1])
        self.x_params.append(observation_params[2])
        self.zoom_params.append(observation_params[3])
        self.states.append(state)
        self.step += 1


class Agent(object):
    checkpoint_path = os.path.join(FLAGS.train_dir, 'model.ckpt')
    replay_memory_path = os.path.join(FLAGS.train_dir, 'replay_memory')

    def __init__(self, sess):
        self._maybe_delete()

        self.cell = tf.nn.rnn_cell.GRUCell(FLAGS.hidden_size)

        self.replay_memory = ReplayMemory(self.replay_memory_path)
        self.current_ep = None  # set in reset()
        self.last_frame = None  # set in reset() and store()

        self.sess = sess

        self.global_step = tf.get_variable(
            'global_step', [],
            dtype='int32',
            initializer=tf.constant_initializer(0),
            trainable=False)
        self.val_step = tf.get_variable('val_step', [],
                                        dtype='int32',
                                        initializer=tf.constant_initializer(0),
                                        trainable=False)

        self._build()
        self._setup_train()

    def _maybe_delete(self):
        # If we aren't resuming and the train dir exists prompt to delete
        if not FLAGS.resume and os.path.isdir(FLAGS.train_dir):
            print "Starting a new training session but %s exists" % FLAGS.train_dir
            sys.stdout.write(
                "Do you want to delete all the files and recreate it [y] ")
            response = raw_input().lower()
            if response == "" or response == "y" or response == "yes":
                import shutil
                print "rm -rf %s" % FLAGS.train_dir
                shutil.rmtree(FLAGS.train_dir)

    def _setup_train(self):
        batchnorm_updates = tf.get_collection(resnet.UPDATE_OPS_COLLECTION)
        batchnorm_updates_op = tf.group(*batchnorm_updates)

        optimizer = tf.train.GradientDescentOptimizer(FLAGS.learning_rate)
        grads = optimizer.compute_gradients(self.loss)
        for grad, var in grads:
            if FLAGS.var_histograms:
                tf.histogram_summary(var.op.name, var)
            if grad is not None and FLAGS.grad_histograms:
                tf.histogram_summary(var.op.name + '/gradients', grad)

        apply_gradient_op = optimizer.apply_gradients(
            grads, global_step=self.global_step)

        self.train_op = tf.group(apply_gradient_op, batchnorm_updates_op)

        self.summary_op = tf.merge_all_summaries()
        self.summary_writer = tf.train.SummaryWriter(FLAGS.train_dir)

        self.saver = tf.train.Saver(tf.all_variables())

        self.sess.run(tf.initialize_all_variables())

        if FLAGS.resume:
            latest = tf.train.latest_checkpoint(FLAGS.train_dir)
            if not latest:
                print "No checkpoint to resume from in", FLAGS.train_dir
                sys.exit(1)
            print "resume", latest
            print "replay memory size:", self.replay_memory.count()
            self.saver.restore(self.sess, latest)

        if len(FLAGS.restore_resnet) > 0:
            print "restoring resnet..."
            resnet_variables_to_restore = tf.get_collection(
                resnet.RESNET_VARIABLES)
            saver = tf.train.Saver(resnet_variables_to_restore)
            # /Users/ryan/src/tensorflow-resnet/tensorflow-resnet-pretrained-20160509/ResNet-L50.ckpt
            saver.restore(self.sess, FLAGS.restore_resnet)
            print "done"

    def q_values(self, x, scope):
        return tf.contrib.layers.fully_connected(x, num_actions,
            weights_regularizer=tf.contrib.layers.l2_regularizer(0.00004), scope=scope)

    def cnn(self, x, is_training):
        return resnet.inference_small(x,
                                      is_training=is_training,
                                      num_classes=None,
                                      use_bias=False,
                                      num_blocks=1)

    def _build(self):
        frame_shape = [None, FLAGS.glimpse_size, FLAGS.glimpse_size, 3]

        self.frames0 = tf.placeholder('float', frame_shape, name='frames0')
        self.frames1 = tf.placeholder('float', frame_shape, name='frames1')
        self.initial_states = tf.placeholder('float', [None, self.cell.state_size], name="initial_states")
        self.is_training = tf.placeholder('bool', [], name='is_training')
        # The following are only used for training phase
        self.are_terminal = tf.placeholder('bool', [None], name='are_terminal')
        self.rewards = tf.placeholder('float', [None], name='rewards')
        self.actions = tf.placeholder('int32', [None], name='actions')

        with tf.variable_scope('cnn') as scope:
            x0 = self.cnn(self.frames0, self.is_training)
            scope.reuse_variables()
            x1 = self.cnn(self.frames1, self.is_training)

        with tf.variable_scope('rnn') as scope:
            out0, states0 = self.cell(x0, self.initial_states)
            scope.reuse_variables()
            out1, states1 = self.cell(x1, states0)

        self.states0 = states0 # needed for act()

        # states shape [batch_size, self.cell.state_size]
        # x shape [batch_size, cell.output_size]

        with tf.variable_scope('fc') as scope:
            q_values0 = self.q_values(out0, scope)
            scope.reuse_variables()
            q_values1 = self.q_values(out1, scope)

        max_q_val0 = tf.reduce_max(q_values0, reduction_indices=[1])
        max_q_val1 = tf.reduce_max(q_values1, reduction_indices=[1])

        max_action0 = tf.argmax(q_values0, 1)
        max_action1 = tf.argmax(q_values1, 1)

        self.max_action0 = max_action0 # needed for act()

        # y_j in the paper is expected_rewards
        terminal = self.rewards
        non_terminal = self.rewards + DQN_GAMMA * max_q_val1
        expected_rewards = tf.select(self.are_terminal, terminal, non_terminal)

        # basically we want q_values0[actions]
        # but tensorflow makes this painfully difficult
        # gather: output[i, :] = params[indices[i], :]
        q_values0_t = tf.transpose(q_values0)
        w = tf.gather(params=q_values0_t, indices=self.actions)
        inferred_rewards = tf.diag_part(w)

        q_loss = tf.square(expected_rewards - inferred_rewards, name='q_loss')
        q_loss = tf.reduce_mean(q_loss) # average over batches

        regularization_losses = tf.get_collection(
            tf.GraphKeys.REGULARIZATION_LOSSES)

        self.loss = tf.add_n([q_loss] + regularization_losses, name='loss')
        tf.scalar_summary('loss', self.loss)

        # loss_avg
        ema = tf.train.ExponentialMovingAverage(0.999, self.global_step)
        tf.add_to_collection(resnet.UPDATE_OPS_COLLECTION,
                             ema.apply([self.loss]))
        self.loss_avg = ema.average(self.loss)
        tf.scalar_summary('loss_avg', self.loss_avg)

        # validation error avg
        ema = tf.train.ExponentialMovingAverage(0.9, self.val_step)
        self.val_accuracy = tf.placeholder('float', [], name='val_accuracy')
        self.val_steps = tf.placeholder('float', [], name='val_steps')
        self.val_stats_apply = tf.group(
            self.val_step.assign_add(1),
            ema.apply([self.val_accuracy, self.val_steps]))

        tf.scalar_summary('val_accuracy_avg', ema.average(self.val_accuracy))
        tf.scalar_summary('val_steps_avg', ema.average(self.val_steps))

    def update_val_stats(self, accuracy, mean_steps):
        self.sess.run(self.val_stats_apply, {
            self.val_accuracy: accuracy,
            self.val_steps: mean_steps,
        })

    def _build_action(self, x, name, num_possible_actions, labels):
        return prob, loss

    def reset(self, observation, observation_params):
        self.last_frame = observation
        self.current_ep = Episode(observation_params)

        # This is the rnn state during forward passes
        # it's updated and used in act()
        self.last_state = np.zeros((self.cell.state_size))

    def act(self, is_training):
        assert self.current_ep.step < FLAGS.max_episode_steps

        i = [self.global_step, self.max_action0, self.states0]
        step, max_action0, states0 = self.sess.run(i, {
            self.is_training: False,
            self.frames0: self.last_frame[np.newaxis, :],
            self.initial_states: self.last_state[np.newaxis,:],
        })

        assert max_action0.shape[0] == 1
        action = max_action0[0]

        assert states0.shape[0] == 1
        self.last_state = states0[0]

        # If we are training, we randomly choose an action with probability
        # dqn_epsilon. dqn_epsilon starts at 1.0 and decays to 0.1 over
        # 1,000,000 steps. So at the beginning we only make random actions
        # while towards the end we determine the action 90% of the time.
        if is_training:
            e = (-0.9 / 1000000) * step + 1.0
            dqn_epsilon = max(e, 0.1)

            #print step, "dqn_epsilon", dqn_epsilon

            # With probability dqn_epsilon select a random action.
            if random.random() < dqn_epsilon:
                action = np.random.randint(0, num_actions)
                return action

        return action

    def store(self, observation, action, reward, done, is_training,
              observation_params):
        ep = self.current_ep

        if is_training:
            ep.store(observation_params, action, reward, self.last_state)

        done = (done or ep.step >= FLAGS.max_episode_steps)

        self.last_frame = observation

        if done:
            ep.done()
            self.last_frame = None
            self.current_ep = None

            log("episode done. num_actions %d num_frames %d" %
                (ep.num_actions, ep.num_frames))

            if is_training:
                self.replay_memory.store(ep)

    def build_batch(self, batch_size):
        frames0 = None
        frames1 = None
        are_terminal = []
        rewards = []
        actions = []
        states = []
        for i in range(batch_size):
            ep = self.replay_memory.sample()
            assert ep._done

            # j is "j" in the dqn paper. so it can be anything but the last.
            j = np.random.randint(0, ep.num_frames - 1)

            log("num frames %d" % ep.num_frames)
            log("rewards " + str(ep.rewards))
            log("actions " + str(ep.actions))
            log("random frame j %d" % j)

            is_terminal = (j + 1 == ep.num_frames - 1)
            reward = ep.rewards[j]
            action = ep.actions[j]
            state = ep.get_state(j)

            if DEBUG and j != 0:
                assert np.linalg.norm(state) > 0.001, "non-initial states shouldn't be zero"

            are_terminal.append(is_terminal)
            rewards.append(reward)
            actions.append(action)
            states.append(state)

            log("is_terminal %d" % is_terminal)
            log("reward %f" % reward)
            log("action %d" % action)
            log("state " + str(state))

            # all frames up to phi_j and phi_j+1
            obvs = ep.obvs[j:j + 2, :]

            f0 = obvs[np.newaxis, 0, :]
            f1 = obvs[np.newaxis, 1, :]

            if i == 0:
                frames0 = f0
                frames1 = f1
            else:
                frames0 = np.concatenate((frames0, f0))
                frames1 = np.concatenate((frames1, f1))

        are_terminal = np.asarray(are_terminal, dtype='bool')
        rewards = np.asarray(rewards, dtype='float')
        actions = np.asarray(actions, dtype='int32')

        assert are_terminal.shape == (batch_size, )
        assert rewards.shape == (batch_size, )
        assert actions.shape == (batch_size, )
        assert frames0.shape == (batch_size, FLAGS.glimpse_size, FLAGS.glimpse_size, 3)
        assert frames1.shape == (batch_size, FLAGS.glimpse_size, FLAGS.glimpse_size, 3)

        return frames0, frames1, are_terminal, rewards, actions, states

    def train(self):
        step = self.sess.run(self.global_step)
        write_summary = (step % 10 == 0 and step > 1)

        # Wait until we have some replay_memory built up before we start
        if self.replay_memory.count() < 2 * FLAGS.batch_size: return

        # Sample random minibatch of transititons
        frames0, frames1, are_terminal, rewards, actions, states = self.build_batch(FLAGS.batch_size)

        i = [self.train_op, self.loss_avg]

        if write_summary:
            i.append(self.summary_op)

        o = self.sess.run(i, {
            self.is_training: True,
            self.frames0: frames0,
            self.frames1: frames1,
            self.are_terminal: are_terminal,
            self.rewards: rewards,
            self.actions: actions,
            self.initial_states: states,
        })

        loss_value = o[1]

        if write_summary:
            summary_str = o[2]
            self.summary_writer.add_summary(summary_str, step)

        if step % 5 == 0:
            print "step %d: %.3f loss avg" % (step, loss_value)

        if step % 1000 == 0 and step > 0:
            print 'save checkpoint'
            self.saver.save(self.sess,
                            self.checkpoint_path,
                            global_step=self.global_step)


def main(_):
    print_flags()

    os.environ[
        "IMAGENET_DIR"] = "/Users/ryan/data/imagenet-small/imagenet-small-train"
    env = gym.make('Attention%d-v0' % FLAGS.glimpse_size)

    os.environ[
        "IMAGENET_DIR"] = "/Users/ryan/data/imagenet-small/imagenet-small-val"
    env_val = gym.make('Attention%d-v0' % FLAGS.glimpse_size)

    #env.monitor.start('/tmp/attention', force=True)

    sess = tf.Session(config=tf.ConfigProto(allow_soft_placement=True,
                                            log_device_placement=False))

    agent = Agent(sess)

    for i_episode in xrange(1, FLAGS.num_episodes):
        observation = env.reset()

        agent.reset(observation, env.backdoor_observation_params())

        for t in xrange(FLAGS.max_episode_steps):
            mode = 'human' if FLAGS.show_train_window else 'rgb_array'
            env.render(mode=mode)
            action = agent.act(is_training=True)
            observation, reward, done, _ = env.step(action)
            #print "action, reward, done", (action, reward, done)
            agent.store(observation=observation,
                        observation_params=env.backdoor_observation_params(),
                        action=action,
                        reward=reward,
                        done=done,
                        is_training=True)
            agent.train()
            if done: break

        if i_episode % 20 == 0:
            validation(agent, env_val)

    #env.monitor.close()


def validation(agent, env_val):
    correct = 0
    total = 10
    lengths = []
    for _ in xrange(0, total):
        observation = env_val.reset()
        agent.reset(observation, env_val.backdoor_observation_params())
        debug_str = ''
        total_reward = 0.0
        for t in xrange(FLAGS.max_episode_steps):
            env_val.render(mode='rgb_array')
            action = agent.act(is_training=False)

            debug_str += action_human_str(action) + ' '

            observation, reward, done, _ = env_val.step(action)
            agent.store(
                observation=observation,
                observation_params=env_val.backdoor_observation_params(),
                action=action,
                reward=reward,
                done=done,
                is_training=False)
            total_reward += reward

            if reward != 0:
                debug_str += "%.1f " % reward
            if done: break
        lengths.append(t + 1)

        if reward > 0:
            correct += 1

        #debug_str += " = %.1f " % total_reward
        print debug_str

    accuracy = float(correct) / total
    agent.update_val_stats(accuracy, np.mean(lengths))
    print "validation accuracy %.2f" % accuracy


def print_flags():
    flags = FLAGS.__dict__['__flags']
    for f in flags:
        print f, flags[f]


def _slice(tensor, index, size):
    # First need to build the begin argument to tf.slice.
    # It should be begin=[index, 0] but TF makes this
    # WAY TOO HARD. TODO FILE A BUG
    y = tf.expand_dims(index, 0)
    z = tf.zeros([1], dtype='int32')
    begin = tf.concat(0, [y, z])

    s = tf.slice(tensor, begin, [1, size])
    return tf.squeeze(s, squeeze_dims=[0])


if __name__ == '__main__':
    tf.app.run()
