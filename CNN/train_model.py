import tensorflow as tf
import time, os, traceback, argparse
from datetime import timedelta
import model, utils, data_gen
import plotting_tools as pt
import numpy as np
import psutil

# config: dict containing every info for the training/testing and 
# for the preprocessing.
# train_data_gen: load in RAM the data when needed

def restore_checkpoint(session, restore_, save_dir):
    try:
        print("Trying to restore last checkpoint ...")
        
        # Use TensorFlow to find the latest checkpoint - if any.
        last_chk_path = tf.train.latest_checkpoint(checkpoint_dir=save_dir)
    
        # Loads the data in the checkpoint.
        restore_.restore(session, save_path=last_chk_path)
    
        # If we get to this point, the checkpoint was successfully loaded.
        print("Restored checkpoint from:", last_chk_path)
        return True
    except:
        # If the above failed for some reason, simply
        # initialize all the variables for the TensorFlow graph.
        print("Failed to restore checkpoint.")
        print(traceback.format_exc())
        return False

def scan_checkpoint_for_vars(checkpoint_path, vars_to_check):
    try:
        check_var_list = tf.train.list_variables(checkpoint_path)
        check_var_list = [x[0] for x in check_var_list]
        check_var_set = set(check_var_list)
        vars_in_checkpoint = [x for x in vars_to_check if x.name[:x.name.index(":")] in check_var_set]
        vars_not_in_checkpoint = [x for x in vars_to_check if x.name[:x.name.index(":")] not in check_var_set]
    except:
        print('Impossible to read the last checkpoint from {}.'.format(checkpoint_path))
        vars_in_checkpoint = None
        vars_not_in_checkpoint = vars_to_check
    return vars_in_checkpoint, vars_not_in_checkpoint

''' From a config file, this function creates the TF graph according to the model
    defined in 'model.py'. The graph is returned at the end.'''
def create_TF_graph(data, training, test_on_training=False):
    G = tf.Graph()
    
    config = utils.config[data]
    model_name = config['model']
    pb_kind = config['pb_kind']
    nb_classes = config['nb_classes']
    loss_weights = config['loss_weights']
    
    with G.as_default():
        
        # Input TF pipeline
        data_generator = data_gen.Data_Gen(data, config, training=(training or test_on_training), max_pic_size=[3000,3000])
        data_generator.create_tf_dataset_and_preprocessing(use_metadata = not training)
        
        if(training):
            dyn_learning_rate = tf.placeholder(dtype=tf.float32, shape=[], name='learning_rate')
        
        # input_data[0] == features (and input_data[1] == labels, if any)
        if(model_name == 'LSTM'):
            input_data, input_seq_length = data_generator.get_next_batch()
        else:
            input_data = data_generator.get_next_batch()        

        it_global = tf.Variable(tf.constant(0, shape=[], dtype=tf.int32), trainable=False, name='global_iterator')
        update_it_global = tf.assign_add(it_global, tf.shape(input_data[0])[0]) 
        if(model_name == 'VGG_16'):
            _model = model.Model('VGG_16', pb_kind=pb_kind,
                                         nb_classes=nb_classes, 
                                         batch_norm=config['batch_norm'], 
                                         dropout_prob=config['dropout_prob'], loss_weights=loss_weights,
                                         training_mode=training)
            _model.build_vgg16_like(input_data[0])
            _model.construct_results(input_data[1])
        elif(model_name == 'LSTM'):
            _model = model.Model('LSTM', pb_kind=pb_kind,
                                         nb_classes=nb_classes,
                                         loss_weights=loss_weights,
                                         training_mode=training)
            _model.build_lstm(input_data[0], input_seq_length)
            _model.construct_results(input_data[1])
        elif(model_name == 'VGG_16_encoder_decoder'):
            _model = model.Model('VGG_16_encoder_decoder', 
                                 pb_kind=pb_kind,
                                 training_mode=True,#bug here... (do not set training_mode=False for test)
                                 batch_norm=config['batch_norm'])
            _model.build_vgg16_encoder_decoder(input_data[0])
            _model.construct_results()
        elif(model_name == 'LRCN'):
            _model = model.Model('LRCN', pb_kind=pb_kind,
                                         nb_classes=nb_classes,
                                         training_mode=training,
                                         dropout_prob=config['dropout_prob'],
                                         regress_threshold=config['regression_threshold'],
                                         loss_weights=loss_weights)
            _model.build_lrcn(input_data[0])
            _model.construct_results(input_data[1])
       
        update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
        
        if(training):
            with tf.control_dependencies(update_ops):
                optimizer = tf.train.MomentumOptimizer(learning_rate=dyn_learning_rate, momentum=0.9)
                grads = optimizer.compute_gradients(_model.loss)
                grad_step = optimizer.apply_gradients(grads)
        
        merged = tf.summary.merge_all()
        
        # Defines the results we want in 'ops'
        if(pb_kind == 'classification'):
            if(training):
                ops = [merged, grad_step, _model.loss, 
                       _model.prob, _model.accuracy_up,
                       _model.precision_up, _model.recall_up, 
                       _model.confusion_matrix_up, 
                       _model.pred]
            else:
                ops = [_model.accuracy_up,
                       _model.precision_up, 
                       _model.recall_up, 
                       _model.confusion_matrix_up, 
                       _model.pred]
                if(model_name == 'LSTM'):
                    ops += [input_data, _model.output]
                elif(model_name == 'VGG_16'):
                    ops += [input_data, _model.spp]
        
        elif(pb_kind == 'encoder'):
            if(training):
                ops = [merged, 
                       grad_step, 
                       _model.loss,
#                       _model.output_1,
                       _model.TV,
                       _model.pool5,
                       _model.input_layer, 
                       _model.output]
            else:
                ops = [_model.loss, 
                       _model.input_layer, 
                       _model.output,
                       _model.conv1_1,
                       _model.pool5,
                       input_data]
        
        elif(pb_kind == 'regression'):
            if(training):
                ops = [merged, 
                       grad_step, 
                       _model.loss,
                       _model.MSE_up,
                       _model.confusion_matrix_up,
                       _model.LSTM,
                       _model.output, 
                       input_data[1]]
            else:
                ops = [_model.loss, _model.MSE_up,
                       _model.confusion_matrix_up,
                       _model.output, input_data[1]]

        # Adds the global iteration counter    
        ops += [update_it_global]
        
        # Defines the metrics we want 
        if(pb_kind == 'classification'):
            metrics_ops = [_model.accuracy, 
                           _model.precision, 
                           _model.recall, 
                           _model.confusion_matrix,
                           _model.accuracy_per_class]
        elif(pb_kind == 'regression'):
            metrics_ops = [_model.MSE, _model.confusion_matrix]
        else:
            metrics_ops = [_model.MSE]
        
        if(training):
            init_ops = [tf.global_variables_initializer(),# All weights
                       tf.local_variables_initializer()] # For metrics
        else:
            init_ops = [tf.local_variables_initializer(), 
                       it_global.initializer,
                       _model.reset_metrics()]
        
        # Restores every variable found in the latest checkpoint that matches our current variables
        last_chk_path = tf.train.latest_checkpoint(checkpoint_dir=config['checkpoint'])
        v_in_chk, v_not_in_chk = scan_checkpoint_for_vars(last_chk_path,  tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES))
        
        if(len(v_not_in_chk) > 0):
            print('Warning: some variables are not found in the latest checkpoint:')
            for v in v_not_in_chk:
                print('\t- {}'.format(v.name))
            print('Default initialization will be used instead.')
        
        restore = tf.train.Saver(v_in_chk) # restore only the found variables
        saver = tf.train.Saver()           # save all the variables

    return (G, data_generator, init_ops, ops, metrics_ops, saver, restore, _model)


def train_model(data):
    
    config = utils.config[data]
    pb_kind = config['pb_kind']
    checkpoint_dir = config['checkpoint']
    tensorboard_dir= config['tensorboard']
    learning_rate = config['learning_rate'] # initial learning rate
    display_plots = config['display']
    #epsilon = config['tolerance'] # useful for updating learning rate
   
    num_epochs = config['num_epochs']
    #checkpoint_iter = config['checkpoint_iter']
    model_name = config['model']
    
    print('Initializing training graph.')
    G, data_generator, init_ops, ops, metrics_ops, saver, restore, model = create_TF_graph(data, training=True)
    sess = tf.Session(graph=G, config=tf.ConfigProto(allow_soft_placement=True,
                                                     intra_op_parallelism_threads=config['num_threads'],
                                                     inter_op_parallelism_threads=config['num_threads']))
    print('Initializing all variables')
    sess.run(init_ops)
    num_tot_files = data_generator.get_num_total_files()
    start = time.time()
    
    with sess.as_default():
        
        restore_checkpoint(sess, restore, checkpoint_dir)
        train_writer = tf.summary.FileWriter(tensorboard_dir, sess.graph)      
        learning_rate = config['learning_rate']
        global_counter = sess.run(G.get_tensor_by_name('global_iterator:0')) 
        for epoch in range(num_epochs):
            # Decreases the learning rate every x epochs
            if(epoch % 40  == 0 and epoch > 0):
                learning_rate = learning_rate/2
            
            batch_it = 0
            end_of_batch = False
            step = 0
            
            while(not end_of_batch):
                # Generates the next batch of data and loads it in memory
                end_of_batch = data_generator.gen_batch_dataset(save_extracted_data=False, 
                                                 retrieve_data=False,
                                                 take_random_files = True,
                                                 get_metadata=False)
                
                num_files = data_generator.get_num_files_analyzed()
                num_pictures = data_generator.get_num_features()
                if(not end_of_batch):
                    
                    # Initializes the iterator on the current batch 
                    sess.run(data_generator.data_iterator.initializer)
                    if(model_name == 'LSTM'):
                        sess.run(data_generator.seq_length_iterator.initializer)
                        
                    # Begins to load the data into the input TF pipeline
                    end_of_data = False
                    num_pics_analyzed = 0
                    
                    while(not end_of_data):
                        try:
                            # Runs the optimization and updates the metrics
                            results = sess.run(ops, feed_dict={G.get_tensor_by_name('learning_rate:0') : learning_rate})
                                                        
                            # Computes the metrics
                            metrics = sess.run(metrics_ops)

                            # Gets the global iteration counter                       
                            num_pics_analyzed += sess.run(G.get_tensor_by_name('global_iterator:0')) - global_counter
                            global_counter = sess.run(G.get_tensor_by_name('global_iterator:0'))
                            step += 1
                            # Plots the variables in TensorBoard
                            train_writer.add_summary(results[0], global_step=results[-1])
                            
                            # Plots in console the metrics we want and hyperparameters
                            if(step % 2 == 0):
                                # Prints the memory usage
                                mem = psutil.virtual_memory()
                                print('Memory info: {0}% used, {1:.2f} GB available, {2:.2f}% active'.format(mem.percent, mem.available/(1024**3), 100*mem.active/mem.total))
                                # Prints the counters
                                print('\nTrain (epoch {}/{}, file {}/{}) [{}/{} {:0.2f}%]\t Loss: {:0.5f}\t'.format(
                                        epoch+1, num_epochs, num_files, num_tot_files, num_pics_analyzed, num_pictures, 100*num_pics_analyzed/num_pictures, results[2]))
                                # Prints the confusion matrix
                                if(pb_kind == 'classification'):
                                    print('\nConfusion matrix: \n{}'.format(metrics[3]))  
                                # Prints the reconstruction 
                                elif(pb_kind == 'encoder' and display_plots):
#                                    print('True TV: {}\tPredicted TV: {}'.format(results[-5], results[-6]))
                                    true_pic = results[-3][0]
                                    rec_pic = results[-2][0]
                                    out_encoder = results[-4][0]
                                    pics = np.concatenate((true_pic, rec_pic), axis=2)
                                    labels = ['True {}'.format(seg) for seg in config['segs']]
                                    labels += ['Reconstructed {}'.format(seg) for seg in config['segs']]
                                    labels_out_encoder = ['Filter {}'.format(k) for k in range(2*len(config['segs']))]
                                    pt.Plotting_Tools.plot_pictures(pics, nrows=2, ncols=len(config['segs']), labels=labels, figsize=(3*len(config['segs']), 4))
                                    pt.Plotting_Tools.plot_pictures(out_encoder, nrows=2, ncols=len(config['segs']), labels=labels_out_encoder, figsize=(3*len(config['segs']), 4))
                                # Prints the true and predicted value(s)
                                elif(pb_kind == 'regression'):
                                    print('Input size: {}'.format(results[-4][0].shape))
                                    if(display_plots):
                                        pic_lrcn = results[-4][0] # 512 filters
                                        pt.Plotting_Tools.plot_pictures(pic_lrcn, nrows=2, ncols=2)
                                    print('Output: {}\t Ground truth: {}'.format(results[-3], results[-2]))
                                    print('Mean Squared Error: {:.3f}'.format(metrics[0]))
                                    print('Confusion matrix [threshold {}]:\n{}'.format(config['regression_threshold'], metrics[1]))

                        except tf.errors.OutOfRangeError:                            
                            end_of_data = True
                    
                    # Saves the weights 
                    saver.save(sess, os.path.join(checkpoint_dir,'training_{}.ckpt'.format(model_name)), global_counter) 
                    print('Weights saved at iteration {}.\n'.format(global_counter))
                    batch_it += 1
            # Re-init the files in the data loader queue after each epoch
            data_generator.init_paths_to_file()

    end = time.time()
    print("Time usage: " + str(timedelta(seconds=int(round(end-start)))))



# Test the model created during the training phase. 
# If 'save_features' == True, saves the features extracted
# the neural network (CNN, LSTM or autoencoder)
            
def test_model(data, test_on_training = False, save_features = False):
    
    config = utils.config[data]
    checkpoint_dir = config['checkpoint']
    model_name = config['model']
    pb_kind = config['pb_kind']
    display_plots = config['display']
    print('Initializing testing graph.')
    if(test_on_training):
        print('Warning: the test will be executed on the training set.')
    G, data_generator, init_ops, ops, metrics_ops, _, restore, model = create_TF_graph(data, training=False, test_on_training=test_on_training)
    
    sess = tf.Session(graph=G, config=tf.ConfigProto(allow_soft_placement=True,
                                                     intra_op_parallelism_threads=config['num_threads'],
                                                     inter_op_parallelism_threads=config['num_threads']))
    with sess.as_default():
        if(restore_checkpoint(sess, restore, checkpoint_dir)):
            
            # Initializes all the metrics.
            sess.run(init_ops) 
            
            # Starts the test on all files
            batch_it = 0
            end_of_batch = False
            pic_counter = 0
            while(not end_of_batch):
                # Generates the next batch of data and loads it in memory
                end_of_batch = data_generator.gen_batch_dataset(save_extracted_data=False, 
                                                                retrieve_data=False,
                                                                take_random_files=False,
                                                                get_metadata=True,
                                                                verbose=False)
                # Initializes the iterator on the current batch 
                sess.run(data_generator.data_iterator.initializer)
                if(model_name == 'LSTM'):
                    sess.run(data_generator.seq_length_iterator.initializer)
                
                # Begins to load the data into the input TF pipeline
                end_of_data = False
                step = 0
                while(not end_of_data):
                    try:
                        # Updates the metrics according to the current batch
                        results = sess.run(ops)
                        
                        # Computes the metrics
                        metrics = sess.run(metrics_ops)
                        
                        # If necessary, save the features extracted in memory
                        if(save_features):
                            features = results[-3]
                            metadata = results[-2][2]
                            data_generator.add_output_features(features, metadata)

                        if(step % 20 == 0):
                            print('Loss: {}'.format(results[0]))
                        
                        if(step % 20 == 0 and pb_kind == 'encoder' and  display_plots):
                            # Prints the reconstruction 
                            true_pic = results[1][0]
                            rec_pic = results[2][0]
                            pics = np.concatenate((true_pic, rec_pic), axis=2)
                            labels = ['True {}'.format(seg) for seg in config['segs']]
                            labels += ['Reconstructed {}'.format(seg) for seg in config['segs']]
                            pt.Plotting_Tools.plot_pictures(pics, nrows=2, ncols=len(config['segs']), labels=labels, figsize=(3*len(config['segs']), 4),
                                                            save_fig=True, name_fig="ex_{}.png".format(pic_counter))
                            pic_counter += 1
                            # Prints the picture after the 1st conv (64 filters)
                            # and the picture after the last conv (512 filters)
                            first_conv_pic = results[3][0]
                            last_conv_pic = results[4][0]
                            labels_first = ['1st filter {}'.format(k) for k in range(64)]
                            labels_last = ['5th filter {}'.format(k) for k in range(512)]
                            pt.Plotting_Tools.plot_pictures(first_conv_pic, nrows=8, ncols=8, labels=labels_first)
                            pt.Plotting_Tools.plot_pictures(last_conv_pic, nrows=10, ncols=10, labels=labels_last)
                            
                        step += 1
                    except tf.errors.OutOfRangeError:                            
                        end_of_data = True
                
                # Gets the global iteration counter (for plots)
                global_counter = sess.run(G.get_tensor_by_name('global_iterator:0'))
                
                # If necessary, dump the features extracted on disk
                if(save_features):
                    data_generator.dump_output_features()
                
                # Plot in the console the current results
                    # Prints the memory usage
                mem = psutil.virtual_memory()
                print('Memory info: {0}% used, {1:.2f} GB available, {2:.2f}% active'.format(mem.percent, mem.available/(1024**3), 100*mem.active/mem.total))
                if(metrics is not None):
                    print('\n\t----- BATCH {} -----\n'.format(batch_it))
                    print('Current counter: {}\n'.format(global_counter))
                    if(pb_kind == 'classification'):
                        print('\nConfusion matrix: \n{}'.format(metrics[3]))  
                        print('Accuracy : {}, Precision : {} \nRecall : {}, Accuracy per class : {}'.
                          format(metrics[0], metrics[1], metrics[2], metrics[4]))
                    elif(pb_kind == 'regression'):
                        print('MSE: {:.5f}'.format(metrics[0]))
                        print('Confusion matrix (threshold {}):\n{}'.format(config['regression_threshold'], metrics[1]))
                    elif(pb_kind == 'encoder'):
                        print('Loss: {}'.format(results[0]))
                        print('MSE: {:.5f}'.format(metrics[0]))
                    # Finally, saves the confusion matrix, if needed.
                    if(pb_kind == 'classification'):
                        if(test_on_training):
                            np.save(checkpoint_dir+'/training_confusion_matrix', metrics[3])
                        else:
                            np.save(checkpoint_dir+'/testing_confusion_matrix', metrics[3])
                    batch_it += 1
                else:
                    print('No metrics to show. We assume there is no data for the test.')
        else:
            print('Impossible to restore the model. Test aborted.')
    
if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("data_type", type=str, help="Set the working data set.", choices=["SF", "SF_encoded", "MNIST", "CIFAR-10"])
    parser.add_argument("--testing", help="Set the mode (training or testing mode).", default=False, action='store_true')
    parser.add_argument("--save_features", help="If this option is enabled, it saves the output features from the training or testing set.", default=False, action='store_true')
    parser.add_argument("--test_on_training", help="If this option and testing mode enabled, it tests the model on the training data set", default=False, action='store_true')
    parser.add_argument("--pb_kind", type=str, help="Set the kind of problem we want to solve", choices=["classification", "regression", "encoder"])
    parser.add_argument("--data_dims", nargs="+", help="Set the dimensions of feature ([H, W, C] for pictures) in the data set. None values accepted.")
    parser.add_argument("--batch_memsize", type=int, help="Set the memory size of each batch loaded in memory. (in MB)")
    parser.add_argument("-m", "--model", type=str, help="Set the neural network model used.", choices=["VGG_16", "LSTM", "VGG_16_encoder_decoder", "LRCN"])
    parser.add_argument("-t", "--num_threads", type=int, help="Set the number of threads used for the preprocessing.")
    parser.add_argument("-c", "--checkpoint", type=str, help="Set the path to the checkpoint directory.")
    parser.add_argument("--tensorboard", type=str, help="Set the path to the tensorboard directory.")
    parser.add_argument("-r", "--resize_method", type=str, help="Set the resizing method.", choices=["NONE", "LIN_RESIZING", "QUAD_RESIZING", "ZERO_PADDING"])
    parser.add_argument("-b", "--batch_size", type=int, help="Set the number of features in each batch used during the training/testing phase.")
    parser.add_argument("-p", "--prefetch_batch_size", type=int, help="Set the number of pre-fetch features in each batch.")
    parser.add_argument("-s", "--subsampling", type=int, help="Set the subsampling value for each videos (only for SF data set).")
    parser.add_argument("-e", "--num_epochs", type=int, help="Set the total number of epochs for the training phase.")
    parser.add_argument("-w", "--loss_weights", nargs=2, type=float, help="Set the weights in the loss function (class imbalance problem).")
    parser.add_argument("--output_features_dir", type=str, help='Set the output directory where the extracted features should be saved.')
    args = parser.parse_args()
    data = args.data_type
    for key, val in args._get_kwargs():
        if(val is not None):
            if(key=='data_dims'): #Special case to accept 'None' values
                data_dims = []
                for k in val:
                    if(k == 'None'):
                        data_dims += [None]
                    else:
                        data_dims += [int(k)]
                utils.config[data][key] = data_dims
            elif(key!='data_type' and key!='testing'):
                utils.config[data][key] = val
    tf.reset_default_graph()
    if(args.testing):
        res = test_model(data, args.test_on_training, args.save_features)
    else:
        res = train_model(data)
