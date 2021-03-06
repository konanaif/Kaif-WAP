"""solver.py"""

import os
import visdom
from tqdm import tqdm

import torch
import torch.optim as optim
import torch.nn.functional as F
import torch.nn as nn
from torchvision.utils import make_grid, save_image



from .utils import DataGather, mkdirs, grid2gif
from .ops_clatent import recon_loss, kl_divergence, permute_dims
from .model import FactorVAE1, FactorVAE2, Discriminator,classifier,paclassifier
from .dataset import return_data
import matplotlib.pyplot as plt
from sklearn.manifold import TSNE
import numpy as np


class GradReverse(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x):
        return x.view_as(x)

    @staticmethod
    def backward(ctx, grad_output):

        return grad_output.neg() *10
        

def grad_reverse(x):
    return GradReverse.apply(x)

class Solver(object):
    def __init__(self, args):
        # Misc
        use_cuda = args.cuda and torch.cuda.is_available()
        self.device = 'cuda' if use_cuda else 'cpu'
        self.name = args.name
        self.max_iter = int(args.max_iter)
        self.print_iter = args.print_iter
        self.global_iter = 0
        self.global_iter_cls = 0
        self.pbar = tqdm(total=self.max_iter)
        self.pbar_cls = tqdm(total=self.max_iter)
   

        # Data
        self.dset_dir = args.dset_dir
        self.dataset = args.dataset
        self.batch_size = args.batch_size
        self.eval_batch_size = args.eval_batch_size
        self.data_loader = return_data(args,0)
        #self.data_loader_eval = return_data(args,2)
        self.data_loader_eval = return_data(args,3)
        

        # Networks & Optimizers
        self.z_dim = args.z_dim
        self.t_dim = args.t_dim
        self.c_dim = args.c_dim
        self.p_dim = args.p_dim
        self.gamma = args.gamma
        self.grl=args.grl
        self.lr_VAE = args.lr_VAE
        self.beta1_VAE = args.beta1_VAE
        self.beta2_VAE = args.beta2_VAE
        
        self.lr_D = args.lr_D
        self.beta1_D = args.beta1_D
        self.beta2_D = args.beta2_D
        self.alpha=args.alpha
        self.beta=args.beta


        self.lr_cls = args.lr_cls
        self.beta1_cls = args.beta1_D
        self.beta2_cls = args.beta2_D


        if args.dataset == 'dsprites':
            self.VAE = FactorVAE1(self.z_dim).to(self.device)
            self.nc = 1
        else:
            self.VAE = FactorVAE2(self.z_dim).to(self.device)
            self.nc = 3
        self.optim_VAE = optim.Adam(self.VAE.parameters(), lr=self.lr_VAE,
                                    betas=(self.beta1_VAE, self.beta2_VAE))
        
        if use_cuda:
            self.pacls=classifier(self.p_dim,2).cuda()
            self.revcls=classifier(self.p_dim,2).cuda()
            self.tcls=classifier(self.t_dim,2).cuda()
            self.trevcls=classifier(self.t_dim,2).cuda()
            

            self.targetcls=classifier(self.t_dim,2).cuda()
            self.pa_target=classifier(self.p_dim,2).cuda()
            self.target_pa=classifier(self.t_dim,2).cuda()
            self.pa_pa=classifier(self.p_dim,2).cuda()
            self.ca_target=classifier(self.c_dim,2).cuda()
            self.ca_pa=classifier(self.c_dim,2).cuda()
            self.fc=nn.Linear(self.c_dim, self.c_dim).cuda()
        else:
            self.pacls=classifier(self.p_dim,2)
            self.revcls=classifier(self.p_dim,2)
            self.tcls=classifier(self.t_dim,2)
            self.trevcls=classifier(self.t_dim,2)
            

            self.targetcls=classifier(self.t_dim,2)
            self.pa_target=classifier(self.p_dim,2)
            self.target_pa=classifier(self.t_dim,2)
            self.pa_pa=classifier(self.p_dim,2)
            self.ca_target=classifier(self.c_dim,2)
            self.ca_pa=classifier(self.c_dim,2)
            self.fc=nn.Linear(self.c_dim, self.c_dim)
        


        self.D = Discriminator(self.z_dim).to(self.device)
        self.optim_D = optim.Adam(self.D.parameters(), lr=self.lr_D,
                                  betas=(self.beta1_D, self.beta2_D))

        self.optim_pacls = optim.Adam(self.pacls.parameters(), lr=self.lr_D)
        
        self.optim_revcls = optim.Adam(self.revcls.parameters(), lr=self.lr_D)

      

        self.optim_tcls = optim.Adam(self.tcls.parameters(), lr=self.lr_D)
        self.optim_trevcls = optim.Adam(self.trevcls.parameters(), lr=self.lr_D)



        self.optim_cls = optim.Adam(self.targetcls.parameters(), lr=self.lr_cls)
        self.optim_pa_target = optim.Adam(self.pa_target.parameters(), lr=self.lr_cls)
        self.optim_target_pa = optim.Adam(self.target_pa.parameters(), lr=self.lr_cls)
        self.optim_pa_pa = optim.Adam(self.pa_pa.parameters(), lr=self.lr_cls)
        self.optim_ca_pa = optim.Adam(self.ca_pa.parameters(), lr=self.lr_cls)
        self.optim_ca_target = optim.Adam(self.ca_target.parameters(), lr=self.lr_cls)
        self.optim_fc= optim.Adam(self.fc.parameters(), lr=self.lr_cls)
      


        

        self.nets = [self.VAE, self.D,self.pacls,self.targetcls,self.revcls,self.pa_target,self.tcls,self.trevcls]

        # Visdom
        self.viz_on = args.viz_on
        self.win_id = dict(D_z='win_D_z', recon='win_recon', kld='win_kld', acc='win_acc')
        self.line_gather = DataGather('iter', 'soft_D_z', 'soft_D_z_pperm', 'recon', 'kld', 'acc')
        self.image_gather = DataGather('true', 'recon')
        if self.viz_on:
            self.viz_port = args.viz_port
            self.viz = visdom.Visdom(port=self.viz_port)
            self.viz_ll_iter = args.viz_ll_iter
            self.viz_la_iter = args.viz_la_iter
            self.viz_ra_iter = args.viz_ra_iter
            self.viz_ta_iter = args.viz_ta_iter
            if not self.viz.win_exists(env=self.name+'/lines', win=self.win_id['D_z']):
                self.viz_init()

        # Checkpoint
        self.ckpt_dir = os.path.join(args.ckpt_dir, args.name)
        self.ckpt_save_iter = args.ckpt_save_iter
        mkdirs(self.ckpt_dir+"/cls")
        mkdirs(self.ckpt_dir+"/vae")
        
        if args.ckpt_load:
            
            self.load_checkpoint(args.ckpt_load)

        # Output(latent traverse GIF)
        self.output_dir = os.path.join(args.output_dir, args.name)
        self.output_save = args.output_save
        mkdirs(self.output_dir)

    def train(self):
        self.net_mode(train=True)

        ones = torch.ones(self.batch_size, dtype=torch.long, device=self.device)
        zeros = torch.zeros(self.batch_size, dtype=torch.long, device=self.device)

        out = False
        


       
        for i_num in range(self.max_iter-self.global_iter) :
            total_pa_num=0
            total_pa_correct_num=0
            total_male_num=0
            total_male_correct=0
            total_female_num=0
            total_female_correct=0    

            total_heavy_num=0
            total_noheavy_num=0
            total_heavy_correct=0
            total_noheavy_correct=0



            total_rev_num=0
            total_rev_correct_num=0
            

            total_t_num=0
            total_t_correct_num=0
            total_t_rev_num=0
            total_t_rev_correct_num=0

            for i, (x_true1,x_true2,heavy_makeup, male) in enumerate(self.data_loader):
                #from PIL import Image
                #from torchvision import transforms
            
                

                

                heavy_makeup=heavy_makeup.to(self.device)
                male=male.to(self.device)
                x_true1 = x_true1.to(self.device)
                x_recon, mu, logvar, z = self.VAE(x_true1)
                vae_recon_loss = recon_loss(x_true1, x_recon)
                vae_kld = kl_divergence(mu, logvar)
 
                D_z = self.D(z)
                vae_tc_loss = (D_z[:, :1] - D_z[:, 1:]).mean()
                
                

                z_reverse=grad_reverse(z.split(self.p_dim,1)[-1])
                #z_reverse=z.split(10,1)[-1]
                reverse_output=self.revcls(z_reverse)
                output=self.pacls(z.split(self.p_dim,1)[-1])

                z_t_reverse=grad_reverse(z.split(self.t_dim,1)[0])
                #z_reverse=z.split(10,1)[-1]
                t_reverse_output=self.trevcls(z_t_reverse)
                t_output=self.tcls(z.split(self.t_dim,1)[0])


                #if i==0:
                #    print(output.argmax(1))
                #    print(t_output.argmax(1))
                #    #print(t_reverse_output.argmax(1))
                
                
               

                rev_correct=(reverse_output.argmax(1)==heavy_makeup).sum().float()
                rev_num=heavy_makeup.size(0)
              
                pa_correct=(output.argmax(1)==male).sum().float()
                pa_num=male.size(0)


                t_correct=(t_output.argmax(1)==heavy_makeup).sum().float()
                t_num=heavy_makeup.size(0)

                t_rev_correct=(t_reverse_output.argmax(1)==male).sum().float()
                t_rev_num=male.size(0)
              


                total_pa_correct_num+=pa_correct
                total_pa_num+=pa_num
                

                total_rev_correct_num+=rev_correct
                total_rev_num+=rev_num

                total_t_correct_num+=t_correct
                total_t_num+=t_num

                total_t_rev_correct_num+=t_rev_correct
                total_t_rev_num+=t_rev_num
             


                total_male_num+=(male==1).sum()
                total_female_num+=(male==0).sum()


                total_heavy_num+=(heavy_makeup==1).sum()
                total_noheavy_num+=(heavy_makeup==0).sum()


                total_male_correct+=((output.argmax(1)==male) * (male==1)).sum()
           
                total_female_correct+=((output.argmax(1)==male) * (male==0)).sum()


                total_heavy_correct+=((t_output.argmax(1)==heavy_makeup) * (heavy_makeup==1)).sum()
           
                total_noheavy_correct+=((t_output.argmax(1)==heavy_makeup) * (heavy_makeup==0)).sum()


                '''
                pa_correct=(output.argm ax(1)==male).sum()
                pa_num=male.size(0)
                
                total_pa_correct_num+=pa_correct
                total_pa_num+=pa_num
                
                total_male_num+=(male==1).sum()
                total_male_correct+=((output.argmax(1)==male)*(male==1)).sum()
                total_female_num+=(male==0).sum()
                total_female_correct+=((output.argmax(1)==male)*(male==0)).sum()
                
                '''
                #weightweight=torch.tensor([1.0,4.0]).cuda()
                #weight_p=torch.tensor([3.5,1.0]).cuda()
                pa_cls=F.cross_entropy(output,male)
                #pa_cls=F.cross_entropy(output,male)
                rev_cls=F.cross_entropy(reverse_output,heavy_makeup)
                


                t_pa_cls=F.cross_entropy(t_output,heavy_makeup)
                t_rev_cls=F.cross_entropy(t_reverse_output,male)
                #t_rev_cls=F.cross_entropy(t_reverse_output,male)

                 

                vae_loss = vae_recon_loss + vae_kld + self.alpha*self.gamma*vae_tc_loss + self.beta*pa_cls+self.beta*t_pa_cls +self.grl*t_rev_cls+self.grl*rev_cls
                

                self.optim_VAE.zero_grad()
                self.optim_pacls.zero_grad()
                self.optim_revcls.zero_grad()
                self.optim_tcls.zero_grad()
                self.optim_trevcls.zero_grad()

                vae_loss.backward(retain_graph=True)

                
                self.optim_VAE.step()
                self.optim_pacls.step()
                self.optim_revcls.step()
                self.optim_tcls.step()
                self.optim_trevcls.step()
                
                x_true2 = x_true2.to(self.device)
                z_prime = self.VAE(x_true2, no_dec=True)
                z_pperm = permute_dims(z_prime,self.t_dim,self.c_dim,self.p_dim).detach()
                
                D_z_pperm = self.D(z_pperm)
                #D_tc_loss = 0.5*(F.cross_entropy(D_z, zeros) + F.cross_entropy(D_z_pperm, ones))
                D_tc_loss = (F.cross_entropy(D_z, zeros) + F.cross_entropy(D_z_pperm, ones))
                
                
                self.optim_D.zero_grad()
                #D_tc_loss.backward()
                self.optim_D.step()
                
            self.pbar.update(1)
            self.global_iter += 1
            pa_acc=float(total_pa_correct_num)/float(total_pa_num)
            rev_acc=float(total_rev_correct_num)/float(total_rev_num)


            t_acc=float(total_t_correct_num)/float(total_t_num)
            t_rev_acc=float(total_t_rev_correct_num)/float(total_t_rev_num)



            male_acc=float(total_male_correct)/float(total_male_num)
            female_acc=float(total_female_correct)/float(total_female_num)
            heavy_acc=float(total_heavy_correct)/float(total_heavy_num)
            noheavy_acc=float(total_noheavy_correct)/float(total_noheavy_num)


            if self.global_iter%self.print_iter == 0:
                self.pbar.write('[{}] vae_recon_loss:{:.3f} vae_kld:{:.3f} vae_tc_loss:{:.3f} D_tc_loss:{:.3f} pa_cls_loss:{:.3f} pa_acc:{:.3f} m_acc:{:.3f}  f_acc:{:.3f} rev_acc:{:.3f} t_acc:{:.3f} h_acc:{:.3f} noh_acc:{:.3f} t_rev_acc:{:.3f}'.format(
                    self.global_iter, vae_recon_loss.item(), vae_kld.item(), vae_tc_loss.item(), D_tc_loss.item(),pa_cls.item(),pa_acc,male_acc,female_acc,rev_acc,t_acc,heavy_acc,noheavy_acc,t_rev_acc))

            if self.global_iter%self.ckpt_save_iter == 0:
                self.save_checkpoint(self.global_iter)
                #self.ckpt_save_iter+=1

            if self.viz_on and (self.global_iter%self.viz_ll_iter == 0):
                soft_D_z = F.softmax(D_z, 1)[:, :1].detach()
                soft_D_z_pperm = F.softmax(D_z_pperm, 1)[:, :1].detach()
                D_acc = ((soft_D_z >= 0.5).sum() + (soft_D_z_pperm < 0.5).sum()).float()
                D_acc /= 2*self.batch_size
                self.line_gather.insert(iter=self.global_iter,
                                        soft_D_z=soft_D_z.mean().item(),
                                        soft_D_z_pperm=soft_D_z_pperm.mean().item(),
                                        recon=vae_recon_loss.item(),
                                        kld=vae_kld.item(),
                                        acc=D_acc.item())
                #viz_ll_iter+=1
            if self.viz_on and (self.global_iter%self.viz_la_iter == 0):
                self.visualize_line()
                self.line_gather.flush()
                #viz_la_iter+=1

            if self.viz_on and (self.global_iter%self.viz_ra_iter == 0):
                self.image_gather.insert(true=x_true1.data.cpu(),
                                            recon=F.sigmoid(x_recon).data.cpu())
                self.visualize_recon()
                self.image_gather.flush()
                #viz_ra_iter+=1

            if self.viz_on and (self.global_iter%self.viz_ta_iter == 0):
                if self.dataset.lower() == '3dchairs':
                    self.visualize_traverse(limit=2, inter=0.5)
                else:
                    self.visualize_traverse(limit=3, inter=2/3)
            

        self.pbar.write("[Training Finished]")
        self.pbar.close()
        
        self.train_cls()


    def train_cls(self):
        self.net_mode(train=True)

        ones = torch.ones(self.batch_size, dtype=torch.long, device=self.device)
        zeros = torch.zeros(self.batch_size, dtype=torch.long, device=self.device)
      
        self.val()
        out = False
        for i_num in range(50):
            
            for i, (x_true1,x_true2,heavy_makeup, male) in enumerate(self.data_loader):
                
                
                for name,param in self.VAE.named_parameters():
                    #if name=='encode.0.weight':
                    #    print(param[0])
                        
                    param.requires_grad=False
                

               
                
                male=male.to(self.device)
                heavy_makeup=heavy_makeup.to(self.device)
             


                x_true1 = x_true1.to(self.device)
      
                x_recon, mu, logvar, z = self.VAE(x_true1)

                
              

                vae_recon_loss = recon_loss(x_true1, x_recon)
                vae_kld = kl_divergence(mu, logvar)
                D_z = self.D(z)
                vae_tc_loss = (D_z[:, :1] - D_z[:, 1:]).mean()
              



                ##################################################################
                
                    
            
             
                #target=self.targetcls(z)
                
                
                pa_target=self.pa_target(z.split(self.p_dim,1)[-1])

                target_pa=self.target_pa(z.split(self.t_dim,1)[0])
                pa_pa=self.pa_pa(z.split(self.p_dim,1)[-1])

                ca_target=self.ca_target(z.split(self.c_dim,1)[1])
                
                filtered=self.fc(z.split(self.c_dim,1)[1])
                
                ca_pa=self.ca_pa(grad_reverse(filtered))


                target=self.targetcls(z.split(self.t_dim,1)[0]+0.05*filtered)
                #target=self.targetcls(z.split(self.t_dim,1)[1]+z.split(self.t_dim,1)[0])
                
                #

                #weight=torch.tensor([1.0,3.0]).cuda()
                #weight_p=torch.tensor([3.5,1.0]).cuda()
                target_cls=F.cross_entropy(target,heavy_makeup)

   

                pa_target_cls=F.cross_entropy(pa_target,heavy_makeup)

                target_pa_cls=F.cross_entropy(target_pa,male)

                pa_pa_cls=F.cross_entropy(pa_pa,male)
                
                #weight=torch.tensor([3.5,1.0]).cuda()
                ca_target_cls=F.cross_entropy(ca_target,heavy_makeup)
                ca_pa_cls= F.cross_entropy(ca_pa,male)
                #ca_pa_cls= F.cross_entropy(ca_pa,male)

              
               
                
                
                vae_loss = vae_recon_loss + vae_kld + self.gamma*vae_tc_loss
                
                target_loss=pa_target_cls+10*target_cls+target_pa_cls+pa_pa_cls+ca_pa_cls

                self.optim_cls.zero_grad()
                self.optim_pa_target.zero_grad()
                self.optim_target_pa.zero_grad()
                self.optim_pa_pa.zero_grad()
                self.optim_ca_pa.zero_grad()
                self.optim_ca_target.zero_grad()
                self.optim_fc.zero_grad()
                
                target_loss.backward()
                self.optim_pa_target.step()
                self.optim_cls.step()
                self.optim_target_pa.step()
                self.optim_pa_pa.step()
                self.optim_ca_pa.step()
                self.optim_ca_target.step()
                self.optim_fc.step()
                
            self.global_iter_cls += 1
            self.pbar_cls.update(1)

            if self.global_iter_cls%self.print_iter == 0:
                acc=((target.argmax(1)==heavy_makeup).sum().float()/len(x_true1)).item()
                pa_acc=((pa_target.argmax(1)==heavy_makeup).sum().float()/len(x_true1)).item()
                self.pbar_cls.write('[{}] vae_recon_loss:{:.3f} vae_kld:{:.3f} vae_tc_loss:{:.3f} target_loss:{:.3f} accuracy:{:.3f}'.format(
                    self.global_iter_cls, vae_recon_loss.item(), vae_kld.item(), vae_tc_loss.item(), target_loss.item(), acc ))

            if self.global_iter_cls%self.ckpt_save_iter == 0:
                self.save_checkpoint_cls(self.global_iter_cls)

            self.val()
            
        self.pbar_cls.write("[Classifier Training Finished]")
        self.pbar_cls.close()

    

    def val(self):
     

        ones = torch.ones(self.batch_size, dtype=torch.long, device=self.device)
        zeros = torch.zeros(self.batch_size, dtype=torch.long, device=self.device)
        total_true=0
        total_num=0
        total_male_heavy=0
        total_male_nonheavy=0
        total_female_heavy=0
        total_female_nonheavy=0
        total_male_heavy_num=0
        total_male_nonheavy_num=0
        total_female_heavy_num=0
        total_female_nonheavy_num=0
        total_pa_num=0
        total_pa_true=0

        total_target_pa_num=0
        total_target_pa_true=0
        total_pa_pa_num=0
        total_pa_pa_true=0
        total_ca_target_true=0
        total_ca_target_num=0
        total_ca_pa_true=0
        total_ca_pa_num=0

       
        demo=0
        total_male=0
        total_female=0
        total_male_pred=0
        total_female_pred=0
        iter=0


        recon_tsum=np.zeros((self.batch_size,64,64,3))
        recon_csum=np.zeros((self.batch_size,64,64,3))
        recon_psum=np.zeros((self.batch_size,64,64,3))
        recon_sum=np.zeros((self.batch_size,64,64,3))
        origin_sum=np.zeros((self.batch_size,64,64,3))





        z_t=[]
        z_m=[]
        z_p=[]
        z_male=[]
        z_female=[]
        for i, (x_true1,x_true2,heavy_makeup, male) in enumerate(self.data_loader_eval):
            
            #for name,param in self.VAE.named_parameters():
            #    param.requires_grad=False

            #for name,param in self.targetcls.named_parameters():
            #    param.requires_grad=False
            
       
           
            
            
            male=male.to(self.device)
            heavy_makeup=heavy_makeup.to(self.device)
            


            x_true1 = x_true1.to(self.device)
            x_recon, mu, logvar, z = self.VAE(x_true1)
            if i<10:  
                for j in range(len(male)):
                    if male[j]==0:
                        if len(z_female)==0:
                            z_female=z.split(20,1)[0][j].unsqueeze(0)
                        else:
                            z_female=torch.cat((z_female,z.split(20,1)[0][j].unsqueeze(0)),0)
                    else:
                        if len(z_male)==0:
                            z_male=z.split(20,1)[0][j].unsqueeze(0)
                        else:
                            z_male=torch.cat((z_male,z.split(20,1)[0][j].unsqueeze(0)),0)


        
              
       

            '''
 
            
            if len(z_t)==0:
                z_t=z.split(20,1)[0]
                z_m=z.split(20,1)[1]
                z_p=z.split(20,1)[2]
            else:
               
                z_t=torch.cat((z_t,z.split(20,1)[0]),0)
                z_m=torch.cat((z_m,z.split(20,1)[1]),0)
                z_p=torch.cat((z_p,z.split(20,1)[2]),0)
            
            '''
        
            '''
            z_t=z.split(20,1)[0].unsqueeze(2).unsqueeze(2)
            z_c=z.split(20,1)[1].unsqueeze(2).unsqueeze(2)
            z_p=z.split(20,1)[2].unsqueeze(2).unsqueeze(2)
            
            noise=torch.zeros(z.size(0),40,1,1).cuda()
            
            

            z_t=torch.cat([z_t,noise],1)
            z_c=torch.cat([z_c,noise],1)
            z_p=torch.cat([z_p,noise],1)

            recon_t=F.sigmoid(self.VAE.decode(z_t))
            recon_c=F.sigmoid(self.VAE.decode(z_c))
            recon_p=F.sigmoid(self.VAE.decode(z_p))
            

            
            recon_tsum+=recon_t.transpose(1,2).transpose(2,3).cpu().detach().numpy()
            recon_csum+=recon_c.transpose(1,2).transpose(2,3).cpu().detach().numpy()
            recon_psum+=recon_p.transpose(1,2).transpose(2,3).cpu().detach().numpy()
            origin_sum+=x_true1.transpose(1,2).transpose(2,3).cpu().detach().numpy()
            recon_sum+=F.sigmoid(x_recon).transpose(1,2).transpose(2,3).cpu().detach().numpy()
            
            
            

            iter+=1





            '''





          
            pa_target=self.pa_target(z.split(self.p_dim,1)[-1])
            target_pa=self.target_pa(z.split(self.t_dim,1)[0])
            pa_pa=self.pa_pa(z.split(self.p_dim,1)[-1])

            ca_target=self.ca_target(z.split(self.c_dim,1)[1])
            

            
            #self.optim_cls.zero_grad()



            filtered=self.fc(z.split(self.c_dim,1)[1])
                
            ca_pa=self.ca_pa(grad_reverse(filtered))


            target=self.targetcls(z.split(self.t_dim,1)[0]+0.05*filtered)
            #target=self.targetcls(z.split(self.t_dim,1)[1])
            
            #+1.0*z.split(self.t_dim,1)[1]    






            male_heavy=(target.argmax(1)==1)*(heavy_makeup==1)*(male==1)
            male_heavy=male_heavy.sum()
            male_heavy_num=((heavy_makeup==1)*(male==1)).sum()

        

            male_nonheavy=(target.argmax(1)==0)*(heavy_makeup==0)*(male==1)
            male_nonheavy=male_nonheavy.sum()
            male_nonheavy_num=((heavy_makeup==0)*(male==1)).sum()

            female_heavy=(target.argmax(1)==1)*(heavy_makeup==1)*(male==0)
            female_heavy=female_heavy.sum()
            female_heavy_num=((heavy_makeup==1)*(male==0)).sum()

            female_nonheavy=(target.argmax(1)==0)*(heavy_makeup==0)*(male==0)
            female_nonheavy=female_nonheavy.sum()
            female_nonheavy_num=((heavy_makeup==0)*(male==0)).sum()



            total_male_heavy+=male_heavy
            total_male_nonheavy+=male_nonheavy
            total_female_heavy+=female_heavy
            total_female_nonheavy+=female_nonheavy
            total_male_heavy_num+=male_heavy_num
            total_male_nonheavy_num+=male_nonheavy_num
            total_female_heavy_num+=female_heavy_num
            total_female_nonheavy_num+=female_nonheavy_num

            total_pa_true+=(pa_target.argmax(1)==heavy_makeup).sum().float()

            total_pa_num+=len(heavy_makeup)




            total_target_pa_true+=(target_pa.argmax(1)==male).sum().float()

            total_target_pa_num+=len(male)

            total_pa_pa_true+=(pa_pa.argmax(1)==male).sum().float()

            total_pa_pa_num+=len(male)




            total_ca_target_true+=(ca_target.argmax(1)==heavy_makeup).sum().float()

            total_ca_target_num+=len(heavy_makeup)

            total_ca_pa_true+=(ca_pa.argmax(1)==male).sum().float()

            total_ca_pa_num+=len(male)

            


 



            
            total_true+=(target.argmax(1)==heavy_makeup).sum().float()
            total_num+=len(x_true1)
            

            total_male+=(male==1).sum()
            total_female+=(male==0).sum()
           
            total_male_pred+=((target.argmax(1)==1)*(male==1)).sum()
            total_female_pred+=((target.argmax(1)==1)*(male==0)).sum()
        

     

        ## Visualize
        #for j in range(1,60):
        #    model=TSNE(learning_rate=100,n_iter=1000,perplexity=j)
        #    z_tsne=torch.cat((z_female,z_male),0)
        #    
        #    transformed=model.fit_transform(z_tsne.cpu().detach().numpy())
        #
        #    for k in range(2):
        #        
        #        
        #        xs=transformed[len(z_female)*k:len(z_female)*(k+1),0]
        #        ys=transformed[len(z_female)*(k):len(z_female)*(k+1),1]
        #    
        #        plt.scatter(xs,ys,label=str(k),s=5)
        #    
        #    plt.savefig("pa"+str(j)+"ta.png",dpi=300)
        #    plt.clf()
        #import pdb;
        #pdb.set_trace()

        
        model=TSNE(learning_rate=100,n_iter=1000)
        #z_tsne=torch.cat((z_t,z_m,z_p),0)
        #transformed=model.fit_transform(z_tsne.cpu().detach().numpy())

        
        
        #for k in range(3):
        #    
        #    
        #    xs=transformed[19712*(k):19712*(k+1),0]
        #    ys=transformed[19712*(k):19712*(k+1),1]
        #
        #    plt.scatter(xs,ys,label=str(k),s=0.1)
            
            #import pdb;pdb.set_trace()
        
        
        


        male_heavy_acc=total_male_heavy.float()/total_male_heavy_num.float()
        male_nonheavy_acc=total_male_nonheavy.float()/total_male_nonheavy_num.float()
        female_heavy_acc=total_female_heavy.float()/total_female_heavy_num.float()
        female_nonheavy_acc=total_female_nonheavy.float()/total_female_nonheavy_num.float()
        '''
        
        plt.imshow(origin_sum.mean(0)/iter)
        plt.savefig('./figure/origin/origin'+str(i)+'.png')
        
        plt.imshow(recon_sum.mean(0)/iter)
        plt.savefig('./figure/recon/recon'+str(i)+'.png')

        plt.imshow(recon_tsum.mean(0)/iter)
        plt.savefig('./figure/target/target'+str(i)+'.png')
        
        plt.imshow(recon_psum.mean(0)/iter)
        plt.savefig('./figure/protected/protected'+str(i)+'.png')
        plt.imshow(recon_csum.mean(0)/iter)
        plt.savefig('./figure/complimentary/complimentarys'+str(i)+'.png')






        '''










        
        print(total_male_heavy_num.item(),total_male_nonheavy_num.item(),total_female_heavy_num.item(),total_female_nonheavy_num.item())
       
       
        print("\nmale_heavy: ",male_heavy_acc.item(),"\tfemale_heavy: ",female_heavy_acc.item())
        print("male_nonheavy: ",male_nonheavy_acc.item(),"\tfemale_nonheavy: ",female_nonheavy_acc.item())
    
        print("Male_prob:",float(total_male_pred)/float(total_male))
        print("feMale_prob:",float(total_female_pred)/float(total_female))
      
        print("DP:",(float(total_male_pred)/float(total_male)-float(total_female_pred)/float(total_female)))



        
        print("eoo(1):", male_heavy_acc.item()-female_heavy_acc.item())
        print("eoo(0):", male_nonheavy_acc.item()-female_nonheavy_acc.item())

        
       
        total_acc=total_true/total_num
        total_pa_acc=total_pa_true/total_pa_num


        total_target_pa_acc=total_target_pa_true/total_target_pa_num
        total_pa_pa_acc=total_pa_pa_true/total_pa_pa_num

        total_ca_pa_acc=total_ca_pa_true/total_ca_pa_num
       
        total_ca_target_acc=total_ca_target_true/total_ca_target_num
        
        print("target->target Accuracy: ",total_acc.item())
        print("PA->target Accuracy: ",total_pa_acc.item())
        print("target->PA Accuracy: ",total_target_pa_acc.item())
        print("PA->PA Accuracy: ",total_pa_pa_acc.item())
        print("CA->target Accuracy: ",total_ca_target_acc.item())
        print("CA->PA Accuracy: ",total_ca_pa_acc.item())
        
        result = {
            'total_true': total_true,
            'total_num': total_num,
            'total_male_heavy': total_male_heavy,
            'total_male_nonheavy': total_male_nonheavy,
            'total_female_heavy': total_female_heavy,
            'total_female_nonheavy': total_female_nonheavy,
            'total_male_heavy_num': total_male_heavy_num,
            'total_male_nonheavy_num': total_male_nonheavy_num,
            'total_female_heavy_num': total_female_heavy_num,
            'total_female_nonheavy_num': total_female_nonheavy_num,
            'total_pa_num': total_pa_num,
            'total_pa_true': total_pa_true,
            'total_target_pa_num': total_target_pa_num,
            'total_target_pa_true': total_target_pa_true,
            'total_pa_pa_num': total_pa_pa_num,
            'total_pa_pa_true': total_pa_pa_true,
            'total_ca_target_true': total_ca_target_true,
            'total_ca_target_num': total_ca_target_num,
            'total_ca_pa_true': total_ca_pa_true,
            'total_ca_pa_num': total_ca_pa_num,
            'demo': demo,
            'total_male': total_male,
            'total_female': total_female,
            'total_male_pred': total_male_pred,
            'total_female_pred': total_male_pred,
            'iter': iter
        }
        
        return result
        


    def visualize_recon(self):
        data = self.image_gather.data
        true_image = data['true'][0]
        recon_image = data['recon'][0]

        true_image = make_grid(true_image)
        recon_image = make_grid(recon_image)
        sample = torch.stack([true_image, recon_image], dim=0)
        self.viz.images(sample, env=self.name+'/recon_image',
                        opts=dict(title=str(self.global_iter)))

    def visualize_line(self):
        data = self.line_gather.data
        iters = torch.Tensor(data['iter'])
        recon = torch.Tensor(data['recon'])
        kld = torch.Tensor(data['kld'])
        D_acc = torch.Tensor(data['acc'])
        soft_D_z = torch.Tensor(data['soft_D_z'])
        soft_D_z_pperm = torch.Tensor(data['soft_D_z_pperm'])
        soft_D_zs = torch.stack([soft_D_z, soft_D_z_pperm], -1)

        self.viz.line(X=iters,
                      Y=soft_D_zs,
                      env=self.name+'/lines',
                      win=self.win_id['D_z'],
                      update='append',
                      opts=dict(
                        xlabel='iteration',
                        ylabel='D(.)',
                        legend=['D(z)', 'D(z_perm)']))
        self.viz.line(X=iters,
                      Y=recon,
                      env=self.name+'/lines',
                      win=self.win_id['recon'],
                      update='append',
                      opts=dict(
                        xlabel='iteration',
                        ylabel='reconstruction loss',))
        self.viz.line(X=iters,
                      Y=D_acc,
                      env=self.name+'/lines',
                      win=self.win_id['acc'],
                      update='append',
                      opts=dict(
                        xlabel='iteration',
                        ylabel='discriminator accuracy',))
        self.viz.line(X=iters,
                      Y=kld,
                      env=self.name+'/lines',
                      win=self.win_id['kld'],
                      update='append',
                      opts=dict(
                        xlabel='iteration',
                        ylabel='kl divergence',))

    def visualize_traverse(self, limit=3, inter=2/3, loc=-1):
        self.net_mode(train=False)

        decoder = self.VAE.decode
        encoder = self.VAE.encode
        interpolation = torch.arange(-limit, limit+0.1, inter)
   
        random_img = self.data_loader.dataset.__getitem__(0)[1]
        random_img = random_img.to(self.device).unsqueeze(0)
        random_img_z = encoder(random_img)[:, :self.z_dim]

        if self.dataset.lower() == 'dsprites':
            fixed_idx1 = 87040 # square
            fixed_idx2 = 332800 # ellipse
            fixed_idx3 = 578560 # heart

            fixed_img1 = self.data_loader.dataset.__getitem__(fixed_idx1)[0]
            fixed_img1 = fixed_img1.to(self.device).unsqueeze(0)
            fixed_img_z1 = encoder(fixed_img1)[:, :self.z_dim]

            fixed_img2 = self.data_loader.dataset.__getitem__(fixed_idx2)[0]
            fixed_img2 = fixed_img2.to(self.device).unsqueeze(0)
            fixed_img_z2 = encoder(fixed_img2)[:, :self.z_dim]

            fixed_img3 = self.data_loader.dataset.__getitem__(fixed_idx3)[0]
            fixed_img3 = fixed_img3.to(self.device).unsqueeze(0)
            fixed_img_z3 = encoder(fixed_img3)[:, :self.z_dim]

            Z = {'fixed_square':fixed_img_z1, 'fixed_ellipse':fixed_img_z2,
                 'fixed_heart':fixed_img_z3, 'random_img':random_img_z}

        elif self.dataset.lower() == 'celeba':
            fixed_idx1 = 70000 # 'CelebA/img_align_celeba/191282.jpg'
            fixed_idx2 = 143307 # 'CelebA/img_align_celeba/143308.jpg'
            fixed_idx3 = 101535 # 'CelebA/img_align_celeba/101536.jpg'
            fixed_idx4 = 70059  # 'CelebA/img_align_celeba/070060.jpg'

            fixed_img1 = self.data_loader.dataset.__getitem__(fixed_idx1)[0]
            fixed_img1 = fixed_img1.to(self.device).unsqueeze(0)
            fixed_img_z1 = encoder(fixed_img1)[:, :self.z_dim]

            fixed_img2 = self.data_loader.dataset.__getitem__(fixed_idx2)[0]
            fixed_img2 = fixed_img2.to(self.device).unsqueeze(0)
            fixed_img_z2 = encoder(fixed_img2)[:, :self.z_dim]

            fixed_img3 = self.data_loader.dataset.__getitem__(fixed_idx3)[0]
            fixed_img3 = fixed_img3.to(self.device).unsqueeze(0)
            fixed_img_z3 = encoder(fixed_img3)[:, :self.z_dim]

            fixed_img4 = self.data_loader.dataset.__getitem__(fixed_idx4)[0]
            fixed_img4 = fixed_img4.to(self.device).unsqueeze(0)
            fixed_img_z4 = encoder(fixed_img4)[:, :self.z_dim]

            Z = {'fixed_1':fixed_img_z1, 'fixed_2':fixed_img_z2,
                 'fixed_3':fixed_img_z3, 'fixed_4':fixed_img_z4,
                 'random':random_img_z}

        elif self.dataset.lower() == '3dchairs':
            fixed_idx1 = 40919 # 3DChairs/images/4682_image_052_p030_t232_r096.png
            fixed_idx2 = 5172  # 3DChairs/images/14657_image_020_p020_t232_r096.png
            fixed_idx3 = 22330 # 3DChairs/images/30099_image_052_p030_t232_r096.png

            fixed_img1 = self.data_loader.dataset.__getitem__(fixed_idx1)[0]
            fixed_img1 = fixed_img1.to(self.device).unsqueeze(0)
            fixed_img_z1 = encoder(fixed_img1)[:, :self.z_dim]

            fixed_img2 = self.data_loader.dataset.__getitem__(fixed_idx2)[0]
            fixed_img2 = fixed_img2.to(self.device).unsqueeze(0)
            fixed_img_z2 = encoder(fixed_img2)[:, :self.z_dim]

            fixed_img3 = self.data_loader.dataset.__getitem__(fixed_idx3)[0]
            fixed_img3 = fixed_img3.to(self.device).unsqueeze(0)
            fixed_img_z3 = encoder(fixed_img3)[:, :self.z_dim]

            Z = {'fixed_1':fixed_img_z1, 'fixed_2':fixed_img_z2,
                 'fixed_3':fixed_img_z3, 'random':random_img_z}
        else:
            fixed_idx = 0
            fixed_img = self.data_loader.dataset.__getitem__(fixed_idx)[0]
            fixed_img = fixed_img.to(self.device).unsqueeze(0)
            fixed_img_z = encoder(fixed_img)[:, :self.z_dim]

            random_z = torch.rand(1, self.z_dim, 1, 1, device=self.device)

            Z = {'fixed_img':fixed_img_z, 'random_img':random_img_z, 'random_z':random_z}

        gifs = []
        for key in Z:
            z_ori = Z[key]
            samples = []
            for row in range(self.z_dim):
                if loc != -1 and row != loc:
                    continue
                z = z_ori.clone()
                for val in interpolation:
                    z[:, row] = val
                    sample = F.sigmoid(decoder(z)).data
                    samples.append(sample)
                    gifs.append(sample)
            samples = torch.cat(samples, dim=0).cpu()
            title = '{}_latent_traversal(iter:{})'.format(key, self.global_iter)
            self.viz.images(samples, env=self.name+'/traverse',
                            opts=dict(title=title), nrow=len(interpolation))

        if self.output_save:
            output_dir = os.path.join(self.output_dir, str(self.global_iter))
            mkdirs(output_dir)
            gifs = torch.cat(gifs)
            gifs = gifs.view(len(Z), self.z_dim, len(interpolation), self.nc, 64, 64).transpose(1, 2)
            for i, key in enumerate(Z.keys()):
                for j, val in enumerate(interpolation):
                    save_image(tensor=gifs[i][j].cpu(),
                               filename=os.path.join(output_dir, '{}_{}.jpg'.format(key, j)),
                               nrow=self.z_dim, pad_value=1)

                grid2gif(str(os.path.join(output_dir, key+'*.jpg')),
                         str(os.path.join(output_dir, key+'.gif')), delay=10)

        self.net_mode(train=True)

    def viz_init(self):
        zero_init = torch.zeros([1])
        self.viz.line(X=zero_init,
                      Y=torch.stack([zero_init, zero_init], -1),
                      env=self.name+'/lines',
                      win=self.win_id['D_z'],
                      opts=dict(
                        xlabel='iteration',
                        ylabel='D(.)',
                        legend=['D(z)', 'D(z_perm)']))
        self.viz.line(X=zero_init,
                      Y=zero_init,
                      env=self.name+'/lines',
                      win=self.win_id['recon'],
                      opts=dict(
                        xlabel='iteration',
                        ylabel='reconstruction loss',))
        self.viz.line(X=zero_init,
                      Y=zero_init,
                      env=self.name+'/lines',
                      win=self.win_id['acc'],
                      opts=dict(
                        xlabel='iteration',
                        ylabel='discriminator accuracy',))
        self.viz.line(X=zero_init,
                      Y=zero_init,
                      env=self.name+'/lines',
                      win=self.win_id['kld'],
                      opts=dict(
                        xlabel='iteration',
                        ylabel='kl divergence',))

    def net_mode(self, train):
        if not isinstance(train, bool):
            raise ValueError('Only bool type is supported. True|False')

        for net in self.nets:
            if train:
                net.train()
            else:
                net.eval()

    def save_checkpoint(self, ckptname='last', verbose=True):
        model_states = {'D':self.D.state_dict(),
                        'VAE':self.VAE.state_dict(),
                        'PACLS':self.pacls.state_dict(),
                        'REVCLS':self.revcls.state_dict(),
                        'T_CLS':self.tcls.state_dict(),
                        'T_REVCLS':self.trevcls.state_dict()}
        optim_states = {'optim_D':self.optim_D.state_dict(),
                        'optim_VAE':self.optim_VAE.state_dict(),
                        'optim_PACLS':self.optim_pacls.state_dict(),
                        'optim_REVCLS':self.optim_revcls.state_dict(),
                        'optim_TCLS':self.optim_tcls.state_dict(),
                        'optim_TREVCLS':self.optim_trevcls.state_dict()}
        states = {'iter':self.global_iter,
                  'model_states':model_states,
                  'optim_states':optim_states}
     
        filepath = os.path.join(self.ckpt_dir+"/vae", str(ckptname))
        with open(filepath, 'wb+') as f:
            torch.save(states, f)
        if verbose:
            self.pbar.write("=> saved checkpoint '{}' (iter {})".format(filepath, self.global_iter))

    
    def save_checkpoint_cls(self, ckptname='last', verbose=True):
        model_states = {'D':self.D.state_dict(),
                        'VAE':self.VAE.state_dict(),
                        'PACLS':self.pacls.state_dict(),
                        'REVCLS':self.revcls.state_dict(),
                        'TCLS':self.targetcls.state_dict(),
                        'VALCLS':self.pa_target.state_dict(),
                        'T_CLS':self.tcls.state_dict(),
                        'T_REVCLS':self.trevcls.state_dict()}
        optim_states = {'optim_D':self.optim_D.state_dict(),
                        'optim_VAE':self.optim_VAE.state_dict(),
                        'optim_Tcls':self.optim_cls.state_dict(),
                        'optim_PACLS':self.optim_pacls.state_dict(),
                        'optim_REVCLS':self.optim_revcls.state_dict(),
                        'optim_TCLS':self.optim_tcls.state_dict(),
                        'optim_TREVCLS':self.optim_trevcls.state_dict(),
                        'optim_VALCLS':self.optim_pa_target.state_dict()}
        states = {'iter':self.global_iter_cls,
                  'model_states':model_states,
                  'optim_states':optim_states}


        filepath = os.path.join(self.ckpt_dir+"/cls", str(ckptname))
        with open(filepath, 'wb+') as f:
            torch.save(states, f)
        if verbose:
            self.pbar.write("=> saved checkpoint '{}' (iter {})".format(filepath, self.global_iter_cls))



    

    def load_checkpoint(self, ckptname='last', verbose=True):
       
        if ckptname == 'last':
            
            ckpts = os.listdir(self.ckpt_dir+'/vae')
            if not ckpts:
                if verbose:
                    self.pbar.write("=> no checkpoint found")
                return
            
            ckpts = [int(ckpt) for ckpt in ckpts]
            ckpts.sort(reverse=True)
            ckptname = str(ckpts[0])
     
        filepath = os.path.join(self.ckpt_dir+'/vae', ckptname)
        if os.path.isfile(filepath):
            with open(filepath, 'rb') as f:
                checkpoint = torch.load(f)

            self.global_iter = checkpoint['iter']
            self.VAE.load_state_dict(checkpoint['model_states']['VAE'])
            self.D.load_state_dict(checkpoint['model_states']['D'])
            self.pacls.load_state_dict(checkpoint['model_states']['PACLS'])
            self.revcls.load_state_dict(checkpoint['model_states']['REVCLS'])
            self.tcls.load_state_dict(checkpoint['model_states']['T_CLS'])
            self.trevcls.load_state_dict(checkpoint['model_states']['T_REVCLS'])
            
            self.optim_VAE.load_state_dict(checkpoint['optim_states']['optim_VAE'])
            self.optim_D.load_state_dict(checkpoint['optim_states']['optim_D'])
            self.optim_pacls.load_state_dict(checkpoint['optim_states']['optim_PACLS'])
            self.optim_revcls.load_state_dict(checkpoint['optim_states']['optim_REVCLS'])
            self.optim_tcls.load_state_dict(checkpoint['optim_states']['optim_TCLS'])
            self.optim_trevcls.load_state_dict(checkpoint['optim_states']['optim_TREVCLS'])
            
            self.pbar.update(self.global_iter)
            if verbose:
                self.pbar.write("=> loaded checkpoint '{} (iter {})'".format(filepath, self.global_iter))
        else:
            if verbose:
                self.pbar.write("=> no checkpoint found at '{}'".format(filepath))


    def load_checkpoint_cls(self, ckptname='last', verbose=True):
       
        if ckptname == 'last':
          
            ckpts = os.listdir(self.ckpt_dir+"/cls")
            if not ckpts:
                if verbose:
                    self.pbar.write("=> no checkpoint found")
                return
            
            ckpts = [int(ckpt) for ckpt in ckpts]
            ckpts.sort(reverse=True)
           
            ckptname =str(ckpts[0])

        filepath = os.path.join(self.ckpt_dir+'/cls', ckptname)
        if os.path.isfile(filepath):
            with open(filepath, 'rb') as f:
                checkpoint = torch.load(f)

            self.global_iter_cls = checkpoint['iter']
            self.VAE.load_state_dict(checkpoint['model_states']['VAE'])
            self.D.load_state_dict(checkpoint['model_states']['D'])
            self.pacls.load_state_dict(checkpoint['model_states']['PACLS'])
            self.revcls.load_state_dict(checkpoint['model_states']['REVCLS'])
            self.targetcls.load_state_dict(checkpoint['model_states']['TCLS'])
            self.pa_target.load_state_dict(checkpoint['model_states']['VALCLS'])
            self.tcls.load_state_dict(checkpoint['model_states']['T_CLS'])
            self.trevcls.load_state_dict(checkpoint['model_states']['T_REVCLS'])

            self.optim_VAE.load_state_dict(checkpoint['optim_states']['optim_VAE'])
            self.optim_D.load_state_dict(checkpoint['optim_states']['optim_D'])
            self.optim_pacls.load_state_dict(checkpoint['optim_states']['optim_PACLS'])
            self.optim_revcls.load_state_dict(checkpoint['optim_states']['optim_REVCLS'])
            self.optim_pa_target.load_state_dict(checkpoint['optim_states']['optim_VALCLS'])
            self.optim_tcls.load_state_dict(checkpoint['optim_states']['optim_TCLS'])
            self.optim_trevcls.load_state_dict(checkpoint['optim_states']['optim_TREVCLS'])
            self.pbar.update(self.global_iter_cls)
            if verbose:
                self.pbar.write("=> loaded checkpoint '{} (iter {})'".format(filepath, self.global_iter_cls))
        else:
            if verbose:
                self.pbar.write("=> no checkpoint found at '{}'".format(filepath))

    
