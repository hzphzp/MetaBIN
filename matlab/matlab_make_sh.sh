matlab -nodisplay -r "make_sh('MetaBIN', 'seokeon_torch16', 'python3 ./tools/train_net.py --config-file ./configs/MetaBIN/', '', 1, 1, 'MetaBIN/cpu1', ''); make_sh('MetaBIN', 'seokeon_torch16', 'python3 ./tools/train_net.py --config-file ./configs/MetaBIN/', '', 3, 1, 'MetaBIN/cpu3', ''); make_sh('MetaBIN', 'seokeon_torch16', 'python3 ./tools/train_net.py --config-file ./configs/MetaBIN/', '', 5, 1, 'MetaBIN/cpu5', ''); exit" 

#  make_sh('MetaReg', 'seokeon_torch16', 'python3 ./tools/train_net.py --config-file ./configs/MetaReg/', '--resume', 1, 1, 'MetaReg/resume1', ''); make_sh('MetaReg', 'seokeon_torch16', 'python3 ./tools/train_net.py --config-file ./configs/MetaReg/', '--resume', 3, 1, 'MetaReg/resume3', ''); make_sh('MetaReg', 'seokeon_torch16', 'python3 ./tools/train_net.py --config-file ./configs/MetaReg/', '--resume', 5, 1, 'MetaReg/resume5', '');

#matlab -nodisplay -r "make_sh('METAv4', 'seokeon_torch16', 'python3 ./tools/train_net.py --config-file ./configs/METAv3/', '', 2, 1, 'cpu2', ''); exit" 
#matlab -nodisplay -r "make_sh('METAv1', 'seokeon_reid', 'python3 ./tools/train_net.py --config-file ./configs/METAv1/', '--resume', 1, 'resume', 'r'); exit" 

# input parameters
# find_file = 'Augmentation'
# conda_id = 'seokeon_reid';
# name_command = python3 ./tools/train_net.py --config-file ./configs/Augmentation/;
# add_name_command = '';
# num_gpu = 1; 
# save_folder = 'gpu1';
# add_txt_name = '';
