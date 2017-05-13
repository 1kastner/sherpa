from __future__ import absolute_import
from __future__ import division
from .schedulers import JobScheduler
from .resultstable import ResultsTable
from .hparam_generators import RandomGenerator, GaussianProcessEI, LatinHypercube
from .utils.monitoring_utils import visualize_hyperband_params, timedcall
from . import Repository
import math
import os


class Algorithm(object):
    def __init__(self, model_function, loss='val_loss',
                 repo_dir='./hyperband_repository', dataset=None,
                 generator_function=None, train_gen_args=None, steps_per_epoch=None,
                 validation_data=None,
                 valid_gen_args=None, validation_steps=None):
        assert model_function and callable(model_function)
        assert dataset or generator_function, "You need to pass either a" \
                                              "dataset array or generator" \
                                              "arguments"

        if generator_function:
            assert steps_per_epoch and validation_steps,\
                "You need to pass the number of batches/steps per epoch for" \
                "training and validation"

            assert callable(generator_function), "Generator needs to be a function"
            assert callable(validation_data), "Validation data must be a function"

        os.makedirs(repo_dir) if not os.path.exists(repo_dir) else None

        self.results_table = ResultsTable(repo_dir)

        repo = Repository(model_function=model_function,
                          dataset=dataset,
                          results_table=self.results_table,
                          dir=repo_dir,
                          loss=loss,
                          generator_function=generator_function,
                          train_gen_args=train_gen_args,
                          steps_per_epoch=steps_per_epoch,
                          validation_data=validation_data,
                          valid_gen_args=valid_gen_args,
                          validation_steps=validation_steps)

        self.scheduler = JobScheduler(repository=repo)
        # Note, if we pass a scheduler we still need to pass the repo to it

    def run(self, *args, **kwargs):
        raise NotImplemented("This function needs to be implemented in child"
                             "classes")


class Hyperband(Algorithm):
    """
    An Algorithm instance initializes the entire pipeline needed to run a
    hyperparameter optimization. The run() method is used to start
    the optimization.

    # Arguments
        model_function: a function that takes a dictionary of hyperparameters
            as its only argument and returns a compiled Keras model object with
            those hyperparameters
        hparam_ranges: a list of Hyperparameter objects
        repo_dir: the directory to store weights and results table in
        loss: which loss to optimize e.g. 'val_loss', 'val_mse' etc.
        dataset: a dataset of the form ((x_train, y_train), (x_valid, y_valid))
            where x_, y_ are NumPy arrays
        generator_function: alternatively to dataset, a generator function can
            be passed. This is a function that returns a generator, not a generator 
            itself.
        train_gen_args: arguments to be passed to generator_function when
            producing a training generator
        steps_per_epoch: number of batches for one epoch of training when
            using a generator
        validation_data: generator function for the validation data, not the generator
        valid_gen_args: arguments to be passed to generator_function when
            producing a validation generator
        validation_steps: number of batches for one epoch of validation when
            using a generator

    # Methods
    Runs the algorithm with **R** maximum epochs per stage and cut factor
    **eta** between stages.

    # run
    Depends on each optimization algorithm. For Hyperband this is:
        R: The maximum epochs per stage. Hyperband has multiple runs each of
            which goes through multiple stages to discard configurations. At each
            of those stages Hyperband will train for a total of R epochs
        eta: The cut-factor. After each stage Hyperband will reduce the number
            of configurations by this factor. The training
            iterations for configurations that move to the next stage increase
            by this factor

    # Example
    ```python
    def my_model(hparams):
        '''Keras model defintion returns compiled Keras model based on hparams'''
        return keras_model

    my_dataset = load_my_dataset()

    my_hparam_ranges = [Hyperparameter(name='learning_rate', distribution='log-uniform', distr_args=(0.0001, 0.1)),
                    Hyperparameter(name='activation', distribution='choice', distr_args=[('sigmoid', 'tanh', 'relu')]),
                    Hyperparameter(name='dropout', distribution='uniform', distr_args=(0., 1.))]


    hband = Hyperband(model_function=my_model,
                    dataset=my_dataset,
                    hparam_ranges=my_hparam_ranges,
                    repo_dir='./my_test_repo')

    results = hband.run(R=20, eta=3)
    ```
    """
    def __init__(self, model_function, hparam_ranges,
                 repo_dir='./hyperband_repository', loss='val_loss',
                 dataset=None,
                 generator_function=None, train_gen_args=None,
                 steps_per_epoch=None, validation_data=None,
                 valid_gen_args=None, validation_steps=None):
        super(self.__class__, self).__init__(model_function=model_function,
                                             loss=loss,
                                        repo_dir=repo_dir,
                                        dataset=dataset,
                                        generator_function=generator_function,
                                        train_gen_args=train_gen_args,
                                        steps_per_epoch=steps_per_epoch,
                                        validation_data=validation_data,
                                        valid_gen_args=valid_gen_args,
                                        validation_steps=validation_steps)
        self.hparam_gen = RandomGenerator(hparam_ranges)

    def run(self, R=20, eta=3):

        total_epochs = visualize_hyperband_params(R=R, eta=eta)

        log_eta = lambda x: math.log(x) / math.log(eta)
        s_max = int(log_eta(R))
        B = (s_max + 1) * R

        for s in reversed(range(s_max + 1)):
            n = int(math.ceil(B / R / (s + 1) * eta ** s))
            r = R * eta ** (-s)

            for i in range(s + 1):
                n_i = int(n * eta ** (-i))
                r_i = int(round(r * eta ** (i)))

                run = s_max - s + 1
                if i == 0:
                    for j in range(1, n_i+1):
                        if s==s_max and i==0 and j==1:
                            self.estimate_time(self.scheduler.submit,
                                               {'run_id': '{}_{}'.format(run,j),
                                                'hparams':
                                                    self.hparam_gen.next(),
                                                'epochs': r_i},
                                               total_epochs=total_epochs,
                                               r_i=r_i)

                        else:
                            self.scheduler.submit(run_id='{}_{}'.format(run,
                                                                        j),
                                                  hparams=self.hparam_gen.next(),
                                                  epochs=r_i)
                else:
                    for run_id in self.results_table.get_k_lowest_from_run(n_i,
                                                                        run):
                        self.scheduler.submit(run_id=run_id, epochs=r_i)

        return self.results_table.get_table()

    @staticmethod
    def estimate_time(f, args, total_epochs, r_i):
        time, result = timedcall(f, args)

        secs = total_epochs * time / r_i
        hrs = secs // 3600
        mins = (secs % 3600) // 60
        print('-' * 100)
        print('\nThe complete Hyperband optimization is '
              'estimated to take {}hrs and {} '
              'mins\n'.format(
            hrs, mins))
        print('-' * 100)


class TemperatureHyperband(Algorithm):
    def __init__(self, model_function, hparam_ranges,
                 repo_dir='./hyperband_repository', loss='val_loss',
                 dataset=None,
                 generator_function=None, train_gen_args=None,
                 steps_per_epoch=None, validation_data=None,
                 valid_gen_args=None, validation_steps=None):
        super(self.__class__, self).__init__(model_function=model_function,
                                             loss=loss,
                                        repo_dir=repo_dir,
                                        dataset=dataset,
                                        generator_function=generator_function,
                                        train_gen_args=train_gen_args,
                                        steps_per_epoch=steps_per_epoch,
                                        validation_data=validation_data,
                                        valid_gen_args=valid_gen_args,
                                        validation_steps=validation_steps)
        self.hparam_gen = RandomGenerator(hparam_ranges)

    def run(self, R=20, eta=3, temperature=1.):
        total_epochs = visualize_hyperband_params(R=R, eta=eta)

        log_eta = lambda x: math.log(x) / math.log(eta)
        s_max = int(log_eta(R))
        B = (s_max + 1) * R

        for s in reversed(range(s_max + 1)):
            n = int(math.ceil(B / R / (s + 1) * eta ** s))
            r = R * eta ** (-s)

            for i in range(s + 1):
                n_i = int(n * eta ** (-i))
                r_i = int(round(r * eta ** (i)))

                run = s_max - s + 1
                if i == 0:
                    for j in range(1, n_i + 1):
                        if s == s_max and i == 0 and j == 1:
                            self.estimate_time(self.scheduler.submit,
                                               {'run_id': '{}_{}'.format(run,j),
                                                'hparams':
                                                    self.hparam_gen.next(),
                                                'epochs': r_i},
                                               total_epochs=total_epochs,
                                               r_i=r_i)

                        else:
                            self.scheduler.submit(run_id='{}_{}'.format(run,
                                                                        j),
                                                  hparams=self.hparam_gen.next(),
                                                  epochs=r_i)
                else:
                    for run_id in self.results_table.sample_k_ids_from_run(n_i, run):
                        self.scheduler.submit(run_id=run_id, epochs=r_i)

        return self.results_table.get_table()

    @staticmethod
    def estimate_time(f, args, total_epochs, r_i):
        time, result = timedcall(f, args)

        secs = total_epochs * time / r_i
        hrs = secs // 3600
        mins = (secs % 3600) // 60
        print('-' * 100)
        print('\nThe complete Hyperband optimization is '
              'estimated to take {}hrs and {} '
              'mins\n'.format(
            hrs, mins))
        print('-' * 100)




class RandomSearch(Algorithm):
    """
    This is in analogue to Hyperband, a regular sequential random search.
    """
    def __init__(self, model_function, hparam_ranges, loss='val_loss',
                 repo_dir='./random_search_repository', dataset=None,
                 generator_function=None, train_gen_args=None,
                 steps_per_epoch=None, validation_data=None,
                 valid_gen_args=None, validation_steps=None):
        super(self.__class__, self).__init__(model_function=model_function,
                                            loss=loss,
                                            repo_dir=repo_dir,
                                            dataset=dataset,
                                            generator_function=generator_function,
                                            train_gen_args=train_gen_args,
                                            steps_per_epoch=steps_per_epoch,
                                            validation_data=validation_data,
                                            valid_gen_args=valid_gen_args,
                                            validation_steps=validation_steps)
        self.hparam_gen = RandomGenerator(hparam_ranges)

    def run(self, num_experiments, num_epochs):
        run = 1
        for id in range(num_experiments):
            self.scheduler.submit(run_id='{}_{}'.format(run, id),
                                  hparams=self.hparam_gen.next(),
                                  epochs=num_epochs)
            print(self.results_table.get_table())

        return self.results_table.get_table()


class BayesianOptimization(Algorithm):
    def __init__(self, model_function, hparam_ranges,
                 repo_dir='./hyperband_repository', loss='val_loss',
                 dataset=None,
                 generator_function=None, train_gen_args=None,
                 steps_per_epoch=None, validation_data=None,
                 valid_gen_args=None, validation_steps=None):
        super(self.__class__, self).__init__(model_function=model_function,
                                             loss=loss,
                                        repo_dir=repo_dir,
                                        dataset=dataset,
                                        generator_function=generator_function,
                                        train_gen_args=train_gen_args,
                                        steps_per_epoch=steps_per_epoch,
                                        validation_data=validation_data,
                                        valid_gen_args=valid_gen_args,
                                        validation_steps=validation_steps)
        self.hparam_gen = GaussianProcessEI(hparam_ranges)

    def run(self, num_experiments, num_epochs):
        run = 1
        for id in range(num_experiments):
            X = self.results_table.get_hparams_df(as_design_matrix=True)
            y = self.results_table.get_column('Loss')
            next_hparams = self.hparam_gen.next(X=X, y=y)
            self.scheduler.submit(run_id='{}_{}'.format(run, id),
                                  hparams=next_hparams,
                                  epochs=num_epochs)
            # print(self.results_table.get_table())

        return self.results_table.get_table()


class Hyperbayes(Algorithm):
    def __init__(self, model_function, hparam_ranges,
                 repo_dir='./hyperbayes_repository', loss='val_loss',
                 dataset=None,
                 generator_function=None, train_gen_args=None,
                 steps_per_epoch=None, validation_data=None,
                 valid_gen_args=None, validation_steps=None):
        super(self.__class__, self).__init__(model_function=model_function,
                                             loss=loss,
                                        repo_dir=repo_dir,
                                        dataset=dataset,
                                        generator_function=generator_function,
                                        train_gen_args=train_gen_args,
                                        steps_per_epoch=steps_per_epoch,
                                        validation_data=validation_data,
                                        valid_gen_args=valid_gen_args,
                                        validation_steps=validation_steps)
        self.hparam_gen = GaussianProcessEI(hparam_ranges)

    def run(self, R=20, eta=3):

        total_epochs = visualize_hyperband_params(R=R, eta=eta)

        log_eta = lambda x: math.log(x) / math.log(eta)
        s_max = int(log_eta(R))
        B = (s_max + 1) * R

        for s in reversed(range(s_max + 1)):
            n = int(math.ceil(B / R / (s + 1) * eta ** s))
            r = R * eta ** (-s)

            for i in range(s + 1):
                n_i = int(n * eta ** (-i))
                r_i = int(round(r * eta ** (i)))

                run = s_max - s + 1
                if i == 0:
                    for j in range(1, n_i+1):
                        X = self.results_table.get_hparams_df(
                            as_design_matrix=True)
                        y = self.results_table.get_column('Loss')
                        next_hparams = self.hparam_gen.next(X=X, y=y)
                        self.scheduler.submit(run_id='{}_{}'.format(run,j),
                                                  hparams=next_hparams,
                                                  epochs=r_i)
                else:
                    for run_id in self.results_table.get_k_lowest_from_run(n_i,
                                                                        run):
                        self.scheduler.submit(run_id=run_id, epochs=r_i)

        return self.results_table.get_table()

class Legoband(Algorithm):
    def __init__(self, model_function, hparam_ranges,
                 repo_dir='./hyperbayes_repository', loss='val_loss',
                 dataset=None,
                 generator_function=None, train_gen_args=None,
                 steps_per_epoch=None, validation_data=None,
                 valid_gen_args=None, validation_steps=None):
        super(self.__class__, self).__init__(model_function=model_function,
                                             loss=loss,
                                        repo_dir=repo_dir,
                                        dataset=dataset,
                                        generator_function=generator_function,
                                        train_gen_args=train_gen_args,
                                        steps_per_epoch=steps_per_epoch,
                                        validation_data=validation_data,
                                        valid_gen_args=valid_gen_args,
                                        validation_steps=validation_steps)
        self.hparam_gen = RandomGenerator(hparam_ranges)

    def run(self, R=20, eta=3):

        total_epochs = visualize_hyperband_params(R=R, eta=eta)

        log_eta = lambda x: math.log(x) / math.log(eta)
        s_max = int(log_eta(R))
        B = (s_max + 1) * R

        for s in reversed(range(s_max + 1)):
            n = int(math.ceil(B / R / (s + 1) * eta ** s))
            r = R * eta ** (-s)

            for i in range(s + 1):
                n_i = int(n * eta ** (-i))
                r_i = int(round(r * eta ** (i)))

                run = s_max - s + 1
                if i == 0:
                    for j in range(1, n_i+1):
                        self.scheduler.submit(run_id='{}_{}'.format(run,j),
                                              hparams=self.hparam_gen.next(),
                                              epochs=r_i)
                        self.grow_distributions(run_id='{}_{}'.format(run,j),
                                                run=run,
                                                epochs=r_i)


                else:
                    for T_j in self.results_table.get_k_lowest_from_run(n_i,
                                                                        run):
                        self.scheduler.submit(run_id=(run, T_j), epochs=r_i)
                        self.grow_distributions(run_id='{}_{}'.format(run,j),
                                                run=run,
                                                epochs=r_i)

        return self.results_table.get_table()

    def grow_distributions(self, run_id, run, epochs):
        best_id = self.results_table.get_k_lowest_from_run(1, run=run)[0]
        amount = epochs if best_id == run_id else -epochs
        hparams = self.results_table.get(run_id=run_id,
                                         parameter='Hparams')
        hparams = eval(hparams)
        self.hparam_gen.grow(hparams, amount)

class NaturalSelection(Algorithm):
    def __init__(self, model_function, hparam_ranges,
                 repo_dir='./natural_selection_repository', loss='val_loss',
                 dataset=None,
                 generator_function=None, train_gen_args=None,
                 steps_per_epoch=None, validation_data=None,
                 valid_gen_args=None, validation_steps=None):
        super(self.__class__, self).__init__(model_function=model_function,
                                             loss=loss,
                                        repo_dir=repo_dir,
                                        dataset=dataset,
                                        generator_function=generator_function,
                                        train_gen_args=train_gen_args,
                                        steps_per_epoch=steps_per_epoch,
                                        validation_data=validation_data,
                                        valid_gen_args=valid_gen_args,
                                        validation_steps=validation_steps)
        self.hparam_gen = LatinHypercube(hparam_ranges)

    def run(self, factor=6, survivors=4):
        id = 1
        for run in range(factor):
            n_i = 2**(factor-1)/(2**run)
            r_i = 2**run
            k = 0 if run==0 else min(survivors, n_i)

            for run_id in self.results_table.get_k_lowest_from_run(int(k),
                                                                   run-1):
                self.scheduler.submit(run_id=run_id, epochs=r_i)
                self.grow_dist(run_id=run_id, epochs=r_i/2)
                self.results_table.set_value(run_id=run_id, col='Run',
                                             value=run)

            for i in range(int(n_i - k)):
                self.scheduler.submit(run_id='{}_{}'.format(run, id),
                                      epochs=r_i,
                                      hparams=self.hparam_gen.next())
                id += 1

        return self.results_table.get_table()

    def grow_dist(self, run_id, epochs):
        hparams = self.results_table.get(run_id=run_id,
                                         parameter='Hparams')
        hparams = eval(hparams)
        self.hparam_gen.grow(hparams=hparams, amount=epochs)
