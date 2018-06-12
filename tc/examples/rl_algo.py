import numpy as np
import ipdb
import torch
import torch.optim as optim
import torch.nn as nn
import torch.utils.data
import torch.nn.functional as F
import ipdb
from itertools import count
from collections import namedtuple
from torch.distributions import Categorical
import time
import tensor_comprehensions as tc
from visdom import Visdom

NB_HYPERPARAMS, INIT_INPUT_SZ = 13, 5
NB_EPOCHS = 10000
BATCH_SZ = 8

viz = Visdom()
win = viz.line(X=np.arange(NB_EPOCHS), Y=np.random.rand(NB_EPOCHS))

code = """
def group_normalization(
    float(N, G, D, H, W) I, float(G, D) gamma, float(G, D) beta)
    -> (O, mean, var)
{
    mean(n, g) +=! I(n, g, r_d, r_h, r_w)
     var(n, g) +=! I(n, g, r_d, r_h, r_w) * I(n, g, r_d, r_h, r_w)

    O(n, g, d, h, w) = gamma(g, d)
      * ( I(n, g, d, h, w) - mean(n, g) * 4 )
      * rsqrt( var(n, g) * 4
            - mean(n, g) * mean(n, g) * 4 * 4
            + 1e-5)
      + beta(g, d)
}
"""

def evalTime(opt):
    global tc_prog, inp, cat_val
    #print(opt)
    #print(cat_val)
    opt = [cat_val[i][opt[i+INIT_INPUT_SZ]] for i in range(NB_HYPERPARAMS)]
    opt = optionsFromVector(opt)
    warmup = 5
    iters  = 20
    for i in range(warmup):
        tc_prog(*inp, options=opt)
        torch.cuda.synchronize()

    liste_t_tc = []
    now = time.clock()
    for i in range(iters):
        before = time.clock()
        tc_prog(*inp, options=opt)
        #tcwavenet(Data)
        torch.cuda.synchronize()
        after = time.clock()
        liste_t_tc.append(after - before)
        torch.cuda.synchronize()
    total_time = (time.clock() - now)
    mean_time = total_time / iters
    return mean_time

def optionsFromVector(vect):
    options = tc.CudaMappingOptions("naive")
    #options.outerScheduleFusionStrategy("Max")
    #options.intraTileScheduleFusionStrategy("Min")
    options.fixParametersBeforeScheduling(vect[2])
    options.tile([vect[3]]) #why list in doc?
    options.unroll(2**vect[4]) #128 is too big? trying 30
    options.matchLibraryCalls(vect[5])
    options.mapToBlocks([vect[6]])
    options.mapToThreads([vect[7]]) #grid?
    options.useSharedMemory(vect[8])
    options.usePrivateMemory(vect[9])
    options.unrollCopyShared(vect[10])
    #options.maxSharedMemory(vect[11]) #todo 40000 / 0 et divs
    options.useReadOnlyCache(vect[12])
    return options

def computeDivs(sz):
    l = []
    for i in range(sz): #or 10?
        l.append((sz+i)//(i+1))
    return l

def getAllDivs(inp, maxp2=31):
    p2 = []
    pp=1
    for i in range(maxp2+1):
        p2.append(pp)
        pp*=2
    l = []
    for elem in inp:
        for sz in elem.shape:
            l += computeDivs(sz)
    return list(set(l+p2))

def computeCat(inp):
    global cat_sz, cat_val
    cat_sz = np.zeros(NB_HYPERPARAMS).astype(int)
    cat_val = []

    divs = getAllDivs(inp)

    cat_val.append([1,2,3])
    cat_val.append([1,2,3])
    cat_val.append([0,1])
    cat_val.append(divs + [0])
    cat_val.append([i for i in range(30)])
    cat_val.append([0,1])
    cat_val.append(divs)
    cat_val.append(divs)
    cat_val.append([0,1])
    cat_val.append([0,1])
    cat_val.append([0,1])
    cat_val.append([0])
    cat_val.append([0,1])

    for i in range(13):
        cat_sz[i] = len(cat_val[i])

SavedAction = namedtuple('SavedAction', ['log_prob', 'value'])

class Predictor(nn.Module):
    def __init__(self, nb_inputs, nb_actions):
        super(Predictor, self).__init__()
        self.affine1 = nn.Linear(nb_inputs, 32)
        self.affine2 = nn.Linear(32, nb_actions)
        self.affine3 = nn.Linear(32, 1)

    def forward(self, x):
        tmp1 = F.relu(self.affine1(x))
        out_action = F.softmax(self.affine2(tmp1))
        out_value = self.affine3(tmp1)
        return out_action, out_value

class FullNetwork(nn.Module):
    def __init__(self, nb_hyperparams, init_input_sz, batch_size):
        super(FullNetwork, self).__init__()
        self.nb_hyperparams = nb_hyperparams
        self.init_input_sz = init_input_sz
        self.nets = [Predictor(init_input_sz + i, int(cat_sz[i])) for i in range(nb_hyperparams)]
        self.nets = nn.ModuleList(self.nets)
        self.saved_actions = [[] for i in range(batch_size)]

    def select_action(self, x, i, batch_id):
        probs, state_value = self.nets[i](x)
        m = Categorical(probs)
        action = m.sample()
        self.saved_actions[batch_id].append(SavedAction(m.log_prob(action), state_value))
        return action.item()

    def forward(self, x, batch_id):
        for i in range(self.nb_hyperparams):
            sym = self.select_action(x, i, batch_id)
            x = torch.cat([x, torch.FloatTensor([sym])])
        return x

N, G, D, H, W = 5, 5, 5, 5, 5
I, gamma, beta = torch.randn(N, G, D, H, W).cuda(), torch.randn(G, D).cuda(), torch.randn(G, D).cuda()

init_input = (I, gamma, beta)
init_input_sz = np.array([N,G,D,H,W])
init_input_sz = torch.from_numpy(init_input_sz).float()

inp = init_input
computeCat(inp)

net = FullNetwork(NB_HYPERPARAMS, INIT_INPUT_SZ, BATCH_SZ)
optimizer = optim.Adam(net.parameters())
eps = np.finfo(np.float32).eps.item()

tc_prog = tc.define(code, name="group_normalization")

def finish_episode(final_rewards):
    saved_actions = net.saved_actions
    policy_losses = [[] for i in range(BATCH_SZ)]
    value_losses = [[] for i in range(BATCH_SZ)]
    final_rewards = torch.tensor(final_rewards)
    #final_rewards = (final_rewards - final_rewards.mean()) / (final_rewards.std() + eps)
    for batch_id in range(BATCH_SZ):
        for (log_prob, value) in saved_actions[batch_id]:
            reward = final_rewards[batch_id] - value.item()
            policy_losses[batch_id].append(-log_prob * reward)
            value_losses[batch_id].append(F.smooth_l1_loss(value, torch.tensor([final_rewards[batch_id]])))
    optimizer.zero_grad()
    vloss = torch.stack([torch.stack(value_losses[i]).sum() for i in range(BATCH_SZ)]).mean()
    ploss = torch.stack([torch.stack(policy_losses[i]).sum() for i in range(BATCH_SZ)]).mean()
    loss = ploss + vloss
    loss.backward()
    optimizer.step()
    del net.saved_actions[:]
    net.saved_actions = [[] for i in range(BATCH_SZ)]
    return vloss.item(), ploss.item()

INTER_DISP = 20

running_reward = -0.5
tab_rewards=[]
best=-0.5
v_losses=[]
p_losses=[]
for i in range(NB_EPOCHS):
    rewards = []
    for j in range(BATCH_SZ):
        out = net(init_input_sz,j)
        reward = -evalTime(out.numpy().astype(int))
        reward=100*reward#+0.45
        rewards.append(reward)
    vloss, ploss = finish_episode(rewards)
    v_losses.append(vloss)
    p_losses.append(ploss)
    best = max(best, np.max(rewards))
    running_reward = best #running_reward * 0.99 + np.mean(rewards) * 0.01
    tab_rewards.append(-running_reward)
    if i % INTER_DISP == 0:
        viz.line(X=np.column_stack((np.arange(i+1), np.arange(i+1), np.arange(i+1))), Y=np.column_stack((np.array(tab_rewards), np.array(v_losses), np.array(p_losses))), win=win, opts=dict(legend=["Minus Running reward", "Value loss", "Policy loss"]))
    print(-running_reward)