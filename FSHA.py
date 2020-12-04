import tensorflow as tf
import numpy as np
import tqdm
import datasets, architectures, styleloss

def distance_data_loss(a,b):
    l = tf.losses.MeanSquaredError()
    return l(a, b)

def distance_data(a,b):
    l = tf.losses.MeanSquaredError()
    return l(a, b)

class FSHA:
    def __init__(self, xpriv, xpub, id_setup, batch_size, hparams):
            input_shape = xpriv.element_spec[0].shape

            # setup dataset
            self.client_dataset = xpriv.batch(batch_size, drop_remainder=True).repeat(-1)
            self.attacker_dataset = xpub.batch(batch_size, drop_remainder=True).repeat(-1)

            ## setup models
            make_f, make_tilde_f, make_decoder, make_D = architectures.SETUPS[id_setup]

            self.f = make_f(input_shape)
            self.tilde_f = make_tilde_f(input_shape)

            assert self.f.output.shape.as_list()[1:] == self.tilde_f.output.shape.as_list()[1:]
            z_shape = self.tilde_f.output.shape.as_list()[1:]

            self.D = make_D(z_shape)
            self.decoder = make_decoder(z_shape, input_shape[-1])

            self.hparams = hparams
            if self.hparams['style_loss']:
                self.style_model = styleloss.makeStyleModel(input_shape)
            else:
                self.style_model = None
            ##

            # setup optimizer
            self.optimizer0 = tf.keras.optimizers.Adam(learning_rate=hparams['lr_f'])
            self.optimizer1 = tf.keras.optimizers.Adam(learning_rate=hparams['lr_tilde'])
            self.optimizer2 = tf.keras.optimizers.Adam(learning_rate=hparams['lr_D'])



    @staticmethod
    def addNoise(x, alpha):
        return x + tf.random.normal(x.shape) * alpha

    @tf.function
    def train_step(self, x_private, x_public, label_private, label_public):

        with tf.GradientTape(persistent=True) as tape:

            #### Virtually, ON THE CLIENT SIDE:
            # clients' smashed data
            z_private = self.f(x_private)
            ####################################


            #### SERVER-SIDE:
            # map to data space (for evaluation and style loss)
            rec_x_private = self.decoder(z_private)
            ## adversarial loss (f's output must similar to \tilde{f}'s output):
            adv_private_logits = self.D(z_private)
            if self.hparams['WGAN']:
                print("Use WGAN loss")
                f_loss = tf.reduce_mean( tf.nn.sigmoid(adv_private_logits) )
            else:
                f_loss = tf.reduce_mean(tf.keras.losses.binary_crossentropy(tf.ones_like(adv_private_logits), adv_private_logits, from_logits=True))
            ##

            # training \tilde{f}
            if 'alpha' in self.hparams:
                print("Add noise to x_public")
                x_public_noise = self.addNoise(x_public, self.hparams['alpha'])
                z_public = self.tilde_f(x_public_noise)
            else:
                z_public = self.tilde_f(x_public)

            # invertibility loss
            rec_x_public = self.decoder(z_public)
            public_rec_loss = distance_data_loss(x_public, rec_x_public)
            tilde_f_loss = public_rec_loss


            style_loss = 0
            if self.style_model:
                print("Regularize with style-loss")
                style_loss = styleloss.getStyleLoss(rec_x_private, rec_x_public, self.style_model) * self.hparams['style_loss']
                f_loss += style_loss


            # discriminator on attacker's feature-space
            adv_public_logits = self.D(z_public)
            if self.hparams['WGAN']:
                loss_discr_true = tf.reduce_mean( tf.nn.sigmoid(adv_public_logits) )
                loss_discr_fake = -tf.reduce_mean( tf.nn.sigmoid(adv_private_logits) )
                # discriminator's loss
                D_loss = loss_discr_true + loss_discr_fake
            else:
                loss_discr_true = tf.reduce_mean(tf.keras.losses.binary_crossentropy(tf.ones_like(adv_public_logits), adv_public_logits, from_logits=True))
                loss_discr_fake = tf.reduce_mean(tf.keras.losses.binary_crossentropy(tf.zeros_like(adv_private_logits), adv_private_logits, from_logits=True))
                # discriminator's loss
                D_loss = (loss_discr_true + loss_discr_fake) / 2

            if 'gradient_penalty' in self.hparams:
                print("Use GP")
                w = float(self.hparams['gradient_penalty'])
                D_gradient_penalty = self.gradient_penalty(z_private, z_public)
                D_loss += D_gradient_penalty * w

            ##################################################################
            ## attack validation #####################
            loss_c_verification = distance_data(x_private, rec_x_private)
            ############################################
            ##################################################################


        # train client's network 
        var = self.f.trainable_variables
        gradients = tape.gradient(f_loss, var)
        self.optimizer0.apply_gradients(zip(gradients, var))

        # train attacker's autoencoder on public data
        var = self.tilde_f.trainable_variables + self.decoder.trainable_variables
        gradients = tape.gradient(tilde_f_loss, var)
        self.optimizer1.apply_gradients(zip(gradients, var))

        # train discriminator
        var = self.D.trainable_variables
        gradients = tape.gradient(D_loss, var)
        self.optimizer2.apply_gradients(zip(gradients, var))


        return f_loss, tilde_f_loss, D_loss, loss_c_verification, style_loss


    def gradient_penalty(self, x, x_gen):
        epsilon = tf.random.uniform([x.shape[0], 1, 1, 1], 0.0, 1.0)
        x_hat = epsilon * x + (1 - epsilon) * x_gen
        with tf.GradientTape() as t:
            t.watch(x_hat)
            d_hat = self.D(x_hat)
        gradients = t.gradient(d_hat, x_hat)
        ddx = tf.sqrt(tf.reduce_sum(gradients ** 2, axis=[1, 2]))
        d_regularizer = tf.reduce_mean((ddx - 1.0) ** 2)
        return d_regularizer


    def attack(self, x_private):
        # smashed data sent from the client:
        z_private = self.f(x_private)
        # recover private data from smashed data
        tilde_x_private = self.decoder(z_private, training=False)

        z_private_control = self.tilde_f(x_private, training=False)
        control = self.decoder(z_private_control)
        return tilde_x_private.numpy(), control.numpy()


    def __call__(self, iterations, log_frequency=500, verbose=False, progress_bar=True):

        n = int(iterations / log_frequency)
        LOG = np.zeros((n, 5))

        iterator = zip(self.client_dataset.take(iterations), self.attacker_dataset.take(iterations))
        if progress_bar:
            iterator = tqdm.tqdm(iterator , total=iterations)

        i, j = 0, 0
        print("RUNNING...")
        for (x_private, label_private), (x_public, label_public) in iterator:
            log = self.train_step(x_private, x_public, label_private, label_public)

            if i == 0:
                VAL = log[3]
            else:
                VAL += log[3] / log_frequency

            if  i % log_frequency == 0:
                LOG[j] = log

                if verbose:
                    print("log--%02d%%-%07d] validation: %0.4f" % ( int(i/iterations*100) ,i, VAL) )

                VAL = 0
                j += 1


            i += 1
        return LOG


#----------------------------------------------------------------------------------------------------------------------

def binary_accuracy(label, logits):
    p = tf.nn.sigmoid(logits)
    predicted = tf.cast( (p > .5), tf.float32)
    correct_prediction = tf.equal(label, predicted)
    return tf.reduce_mean(tf.cast(correct_prediction, tf.float32))

def classification_loss(label, logits):
    return tf.reduce_mean(tf.keras.losses.binary_crossentropy(label, logits, from_logits=True))


class FSHA_binary_property(FSHA):

    @tf.function
    def train_step(self, x_private, x_public, label_private, label_public):

        with tf.GradientTape(persistent=True) as tape:

            #### Virtually, ON THE CLIENT SIDE:
            # clients' smashed data
            z_private = self.f(x_private)
            ####################################

            #### SERVER-SIDE:
            # map to data space (for evaluation and style loss)
            clss_private_logits = self.decoder(z_private)
            ## adversarial loss (f's output must similar to \tilde{f}'s output):
            adv_private_logits = self.D(z_private)
            if self.hparams['WGAN']:
                print("Use WGAN loss")
                f_loss = tf.reduce_mean( tf.nn.sigmoid(adv_private_logits) )
            else:
                f_loss = tf.reduce_mean(tf.keras.losses.binary_crossentropy(tf.ones_like(adv_private_logits), adv_private_logits, from_logits=True))
            ##

            # attacker's classifier
            z_public = self.tilde_f(x_public)
            clss_public_logits = self.decoder(z_public)

            # classificatio loss
            public_classification_loss = classification_loss(label_public, clss_public_logits)
            public_classification_accuracy = binary_accuracy(label_public, clss_public_logits)
            tilde_f_loss = public_classification_loss

            # discriminator on attacker's feature-space
            adv_public_logits = self.D(z_public)
            if self.hparams['WGAN']:
                loss_discr_true = tf.reduce_mean( tf.nn.sigmoid(adv_public_logits) )
                loss_discr_fake = -tf.reduce_mean( tf.nn.sigmoid(adv_private_logits) )
                # discriminator's loss
                D_loss = loss_discr_true + loss_discr_fake
            else:
                loss_discr_true = tf.reduce_mean(tf.keras.losses.binary_crossentropy(tf.ones_like(adv_public_logits), adv_public_logits, from_logits=True))
                loss_discr_fake = tf.reduce_mean(tf.keras.losses.binary_crossentropy(tf.zeros_like(adv_private_logits), adv_private_logits, from_logits=True))
                # discriminator's loss
                D_loss = (loss_discr_true + loss_discr_fake) / 2

            if 'gradient_penalty' in self.hparams:
                print("Use GP")
                w = float(self.hparams['gradient_penalty'])
                D_gradient_penalty = self.gradient_penalty(z_private, z_public)
                D_loss += D_gradient_penalty * w

            ##################################################################
            ## attack validation #####################
            private_classification_accuracy = binary_accuracy(label_private, clss_private_logits)
            ############################################
            ##################################################################


        var = self.f.trainable_variables
        gradients = tape.gradient(f_loss, var)
        self.optimizer0.apply_gradients(zip(gradients, var))

        var = self.tilde_f.trainable_variables + self.decoder.trainable_variables
        gradients = tape.gradient(tilde_f_loss, var)
        self.optimizer1.apply_gradients(zip(gradients, var))

        var = self.D.trainable_variables
        gradients = tape.gradient(D_loss, var)
        self.optimizer2.apply_gradients(zip(gradients, var))


        return f_loss, tilde_f_loss, D_loss, private_classification_accuracy, public_classification_accuracy
