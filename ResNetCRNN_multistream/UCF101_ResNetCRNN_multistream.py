import os
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision.models as models
import torchvision.transforms as transforms
import torch.utils.data as data
import torchvision
from torch.autograd import Variable
import matplotlib.pyplot as plt
from functions import *
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import OneHotEncoder, LabelEncoder
from sklearn.metrics import accuracy_score
import pickle

# set path
data_path = {'spatial': './jpegs_256/',
             'motion_x': './tvl1_flow/u/',
             'motion_y': './tvl1_flow/v/'}     # UCF-101 data path
action_name_path = './UCF101actions.pkl'
save_model_path = "./ResNetCRNN_ckpt/multistream/"

# EncoderCNN architecture
CNN_fc_hidden1, CNN_fc_hidden2 = 1024, 768
CNN_embed_dim = 512   # latent dim extracted by 2D CNN
res_size = 224        # ResNet image size
dropout_p = 0.0       # dropout probability

# DecoderRNN architecture
RNN_hidden_layers = 3
RNN_hidden_nodes = 512
RNN_FC_dim = 256

# training parameters
k = 101             # number of target category
epochs = 120        # training epochs
batch_size = 45  
learning_rate = 1e-3
log_interval = 10   # interval for displaying training info

# Select which frame to begin & end in videos
begin_frame, end_frame, skip_frame = 1, 29, 1


def train(log_interval, model, device, train_loader, optimizer, epoch):
    # set model as training mode
    spatial_encoder, motion_encoder, rnn_decoder = model
    spatial_encoder.train()
    motion_encoder.train()
    rnn_decoder.train()

    losses = []
    scores = []
    N_count = 0   # counting total trained sample in one epoch
    for batch_idx, ([X1, X2, X3], y) in enumerate(train_loader):
        # distribute data to device
        X1, X2, X3, y = X1.to(device), X2.to(device), X3.to(device), y.to(device).view(-1, )
        '''(X1, X2, X3) = (spatial, motion x, motion y) images'''

        optimizer.zero_grad()
        X1, X2, X3 = spatial_encoder(X1), motion_encoder(X2), motion_encoder(X3)
        X = torch.cat((X1, X2, X3), dim=2)     # concat all streams
        output = rnn_decoder(X)   # output has dim = (batch, number of classes)

        loss = F.cross_entropy(output, y)
        losses.append(loss.item())

        # to compute accuracy
        y_pred = torch.max(output, 1)[1]  # y_pred != output
        step_score = accuracy_score(y.cpu().data.squeeze().numpy(), y_pred.cpu().data.squeeze().numpy())
        scores.append(step_score)         # computed on CPU

        loss.backward()
        optimizer.step()
        N_count += X.size(0)

        # show information
        if (batch_idx + 1) % log_interval == 0:
            print('Train Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}, Accu: {:.2f}%'.format(
                epoch + 1, N_count, len(train_loader.dataset), 100. * (batch_idx + 1) / len(train_loader), loss.item(), 100 * step_score))

    return losses, scores


def validation(model, device, optimizer, test_loader):
    # set model as testing mode
    spatial_encoder, motion_encoder, rnn_decoder = model
    spatial_encoder.eval()
    motion_encoder.eval()
    rnn_decoder.eval()

    test_loss = 0
    all_y = []
    all_y_pred = []
    with torch.no_grad():
        for [X1, X2, X3], y in test_loader:
            # distribute data to device
            X1, X2, X3, y = X1.to(device), X2.to(device), X3.to(device), y.to(device).view(-1, )
            X1, X2, X3 = spatial_encoder(X1), motion_encoder(X2), motion_encoder(X3)
            X = torch.cat((X1, X2, X3), dim=2)     # concat all streams
            output = rnn_decoder(X)   # output has dim = (batch, number of classes)

            loss = F.cross_entropy(output, y, reduction='sum')
            test_loss += loss.item()                 # sum up batch loss
            y_pred = output.max(1, keepdim=True)[1]  # (y_pred != output) get the index of the max log-probability

            # collect all y and y_pred in all batches
            all_y.extend(y)
            all_y_pred.extend(y_pred)

    test_loss /= len(test_loader.dataset)

    # compute accuracy
    all_y = torch.stack(all_y, dim=0)
    all_y_pred = torch.stack(all_y_pred, dim=0)
    test_score = accuracy_score(all_y.cpu().data.squeeze().numpy(), all_y_pred.cpu().data.squeeze().numpy())

    # show information
    print('\nTest set ({:d} samples): Average loss: {:.4f}, Accuracy: {:.2f}%\n'.format(len(all_y), test_loss, 100* test_score))

    # save Pytorch models of best record
    torch.save(spatial_encoder.state_dict(), os.path.join(save_model_path, 'spatial_encoder_epoch{}.pth'.format(epoch + 1)))  # save spatial_encoder
    torch.save(motion_encoder.state_dict(), os.path.join(save_model_path, 'motion_encoder_epoch{}.pth'.format(epoch + 1)))    # save motion_encoder
    torch.save(rnn_decoder.state_dict(), os.path.join(save_model_path, 'rnn_decoder_epoch{}.pth'.format(epoch + 1)))          # save RNN_decoder
    torch.save(optimizer.state_dict(), os.path.join(save_model_path, 'optimizer_epoch{}.pth'.format(epoch + 1)))              # save optimizer
    print("Epoch {} model saved!".format(epoch + 1))

    return test_loss, test_score


# Detect devices
use_cuda = torch.cuda.is_available()                   # check if GPU exists
device = torch.device("cuda" if use_cuda else "cpu")   # use CPU or GPU

# Data loading parameters
params = {'batch_size': batch_size, 'shuffle': True, 'num_workers': 4, 'pin_memory': True} if use_cuda else {}

# load UCF101 actions names
with open(action_name_path, 'rb') as f:
    action_names = pickle.load(f)

# convert labels -> category
le = LabelEncoder()
le.fit(action_names)

# show how many classes there are
list(le.classes_)

# convert category -> 1-hot
action_category = le.transform(action_names).reshape(-1, 1)
enc = OneHotEncoder()
enc.fit(action_category)

# # example
# y = ['HorseRace', 'YoYo', 'WalkingWithDog']
# y_onehot = labels2onehot(enc, le, y)
# y2 = onehot2labels(le, y_onehot)

actions = []
fnames = os.listdir(data_path['spatial'])

all_names = []
for f in fnames:
    loc1 = f.find('v_')
    loc2 = f.find('_g')
    actions.append(f[(loc1 + 2): loc2])

    all_names.append(f)


# list all data files
all_X_list = all_names                  # all video file names
all_y_list = labels2cat(le, actions)    # all video labels

# train, test split
train_list, test_list, train_label, test_label = train_test_split(all_X_list, all_y_list, test_size=0.25, random_state=42)

transform = transforms.Compose([transforms.Resize([res_size, res_size]),
                                transforms.ToTensor(),
                                transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])])

selected_frames = np.arange(begin_frame, end_frame, skip_frame).tolist()

train_set, valid_set = Dataset_CRNN_multistream(data_path, train_list, train_label, selected_frames, transform=transform), \
                       Dataset_CRNN_multistream(data_path, test_list, test_label, selected_frames, transform=transform)

train_loader = data.DataLoader(train_set, **params)
valid_loader = data.DataLoader(valid_set, **params)


# Create model
spatial_encoder = ResCNNEncoder(fc_hidden1=CNN_fc_hidden1, fc_hidden2=CNN_fc_hidden2, drop_p=dropout_p, CNN_embed_dim=CNN_embed_dim).to(device)
motion_encoder = ResCNNEncoder(fc_hidden1=CNN_fc_hidden1, fc_hidden2=CNN_fc_hidden2, drop_p=dropout_p, CNN_embed_dim=CNN_embed_dim).to(device)
rnn_decoder = DecoderRNN(CNN_embed_dim=3*CNN_embed_dim, h_RNN_layers=RNN_hidden_layers, h_RNN=RNN_hidden_nodes, 
                         h_FC_dim=RNN_FC_dim, drop_p=dropout_p, num_classes=k).to(device)

# Parallelize model to multiple GPUs
if torch.cuda.device_count() > 1:
    # for multiple GPUs
    print("Using", torch.cuda.device_count(), "GPUs!")
    spatial_encoder = nn.DataParallel(spatial_encoder)
    motion_encoder = nn.DataParallel(motion_encoder)
    rnn_decoder = nn.DataParallel(rnn_decoder)

    crnn_params = list(spatial_encoder.module.fc1.parameters()) + list(spatial_encoder.module.bn1.parameters()) + \
                  list(spatial_encoder.module.fc2.parameters()) + list(spatial_encoder.module.bn2.parameters()) + \
                  list(spatial_encoder.module.fc3.parameters()) + \
                  list(motion_encoder.module.fc1.parameters()) + list(motion_encoder.module.bn1.parameters()) + \
                  list(motion_encoder.module.fc2.parameters()) + list(motion_encoder.module.bn2.parameters()) + \
                  list(motion_encoder.module.fc3.parameters()) + list(rnn_decoder.parameters())

elif torch.cuda.device_count() == 1:
    # for single GPU
    print("Using", torch.cuda.device_count(), "GPU!")
    crnn_params = list(spatial_encoder.fc1.parameters()) + list(spatial_encoder.bn1.parameters()) + \
                  list(spatial_encoder.fc2.parameters()) + list(spatial_encoder.bn2.parameters()) + \
                  list(spatial_encoder.fc3.parameters()) + \
                  list(motion_encoder.fc1.parameters()) + list(motion_encoder.bn1.parameters()) + \
                  list(motion_encoder.fc2.parameters()) + list(motion_encoder.bn2.parameters()) + \
                  list(motion_encoder.fc3.parameters()) + list(rnn_decoder.parameters())

optimizer = torch.optim.Adam(crnn_params, lr=learning_rate)


# record training process
epoch_train_losses = []
epoch_train_scores = []
epoch_test_losses = []
epoch_test_scores = []

# start training
for epoch in range(epochs):
    # train, test model
    train_losses, train_scores = train(log_interval, [spatial_encoder, motion_encoder, rnn_decoder], device, train_loader, optimizer, epoch)
    epoch_test_loss, epoch_test_score = validation([spatial_encoder, motion_encoder, rnn_decoder], device, optimizer, valid_loader)

    # save results
    epoch_train_losses.append(train_losses)
    epoch_train_scores.append(train_scores)
    epoch_test_losses.append(epoch_test_loss)
    epoch_test_scores.append(epoch_test_score)

    # save all train test results
    A = np.array(epoch_train_losses)
    B = np.array(epoch_train_scores)
    C = np.array(epoch_test_losses)
    D = np.array(epoch_test_scores)
    np.save('./ResNetCRNN_multistream_epoch_training_losses.npy', A)
    np.save('./ResNetCRNN_multistream_epoch_training_scores.npy', B)
    np.save('./ResNetCRNN_multistream_epoch_test_loss.npy', C)
    np.save('./ResNetCRNN_multistream_epoch_test_score.npy', D)

# plot
fig = plt.figure(figsize=(10, 4))
plt.subplot(121)
plt.plot(np.arange(1, epochs + 1), A[:, -1])  # train loss (on epoch end)
plt.plot(np.arange(1, epochs + 1), C)         #  test loss (on epoch end)
plt.title("model loss")
plt.xlabel('epochs')
plt.ylabel('loss')
plt.legend(['train', 'test'], loc="upper left")
# 2nd figure
plt.subplot(122)
plt.plot(np.arange(1, epochs + 1), B[:, -1])  # train accuracy (on epoch end)
plt.plot(np.arange(1, epochs + 1), D)         #  test accuracy (on epoch end)
plt.title("training scores")
plt.xlabel('epochs')
plt.ylabel('accuracy')
plt.legend(['train', 'test'], loc="upper left")
title = "./fig_UCF101_ResNetCRNN_multistream.png"
plt.savefig(title, dpi=600)
# plt.close(fig)
plt.show()
