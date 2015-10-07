import logging
log = logging.getLogger(__name__)
from pylearn2.datasets.dense_design_matrix import DenseDesignMatrix
import numpy as np
from braindecode.datasets.bbci_dataset import BBCIDataset
from braindecode.datasets.sensor_positions import (sort_topologically)
from wyrm.processing import select_channels, select_epochs
from braindecode.datasets.filterbank import generate_filterbank
from braindecode.mywyrm.processing import (bandpass_cnt,  segment_dat_fast)
from wyrm.types import Data
from scipy.signal import blackmanharris
from pylearn2.format.target_format import OneHotFormatter

class BBCIPylearnDataset(DenseDesignMatrix):
    """ This loads BBCI datasets and puts them in a Dense Design Matrix. 
    Load sensor names will only load these sensors and also only perform 
    cleaning using these sensors(!). So you will get different cleaning results."""
    def __init__(self, filenames, load_sensor_names=None,
        sensor_names = None,
        cnt_preprocessors=[], epo_preprocessors=[],
        limits=None, start=None, stop=None,
        axes=('b', 'c', 0, 1),
        segment_ival=[0,4000],
        unsupervised_preprocessor=None):

        # sort sensors topologically to allow networks to exploit topology
        if sensor_names is not None:
            sensor_names = sort_topologically(sensor_names)
        if load_sensor_names is not None:
            load_sensor_names = sort_topologically(load_sensor_names)
        if load_sensor_names is not None and sensor_names is not None:
            for sensor_name in sensor_names:
                assert sensor_name in load_sensor_names, ("Cannot select "
                    "sensor {:s} later as it will not be loaded.").format(
                        sensor_name)
        self.__dict__.update(locals())
        del self.self
        filename = filenames # for now only for one file
        self.bbci_set = BBCIDataset(filename, 
            sensor_names=load_sensor_names, #!! other sensors will not be used during cleaning
            cnt_preprocessors=cnt_preprocessors, 
            epo_preprocessors=epo_preprocessors,
            segment_ival=segment_ival)
        self._data_not_loaded_yet = True # needed for lazy loading
        
    def load(self):
        self.load_bbci_set()
        self.create_dense_design_matrix()
        self.remove_bbci_set_epo()
        if self.unsupervised_preprocessor is not None:
            self.apply_unsupervised_preprocessor()
        self._data_not_loaded_yet = False # needed for lazy loading
      
    def load_bbci_set(self):  
        raise NotImplementedError("Should not be called anymore, only call"
            " clean dataset loader")

    def create_dense_design_matrix(self):
        # epo has original shape trials x samples x channels x(freq/band?)
        topo_view = self.bbci_set.epo.data.swapaxes(1,2).astype(np.float32)
        
        
        # add empty axes if needed
        if topo_view.ndim == 3:
            topo_view = np.expand_dims(topo_view, axis=3)
        #TODO(Robin): debug/remove again
        topo_view = np.ascontiguousarray(np.copy(topo_view))
        y = [event_class for time, event_class in self.bbci_set.epo.markers]
        assert np.array_equal(self.bbci_set.epo.axes[0] + 1, y), ("trial axes should"
            "have same event labels (offset by 1 due to 0 and 1-based indexing")

        topo_view, y = self.adjust_for_start_stop_limits(topo_view, y)

        y = format_targets(np.array(y))
        super(BBCIPylearnDataset, self).__init__(topo_view=topo_view, y=y, 
                                              axes=self.axes)
        
        log.info("Loaded dataset with shape: {:s}".format(
            str(self.get_topological_view().shape)))
    
    def remove_bbci_set_epo(self):
        """ To save memory, delete bbci set data,
        but keep bbci_set itself to allow reloading. """
        del self.bbci_set.epo

    def adjust_for_start_stop_limits(self, topo_view, y):
        # cant use both limits and start and stop...
        assert(self.limits is None) or \
            (self.start is None and self.stop is None)
        if (self.limits is None):
            start, stop = adjust_start_stop(topo_view.shape[0], 
                self.start, self.stop)
            new_topo_view = topo_view[start:stop]
            new_y = y[start:stop]
        else:
            start = self.limits[0][0]
            stop = self.limits[0][1]
            new_topo_view = topo_view[start:stop]
            new_y = y[start:stop]
            for limit in self.limits[1:]:
                start = limit[0]
                stop = limit[1]
                topo_view_part = topo_view[start:stop]
                y_part = y[start:stop]
                new_topo_view = np.append(new_topo_view, topo_view_part, 0)
                new_y = np.append(new_y, y_part, 0)
        return new_topo_view, new_y

    def apply_unsupervised_preprocessor(self):
        self.unsupervised_preprocessor.apply(self, can_fit=False)
        log.info("Applied unsupervised preprocessing, dataset shape now: {:s}".format(
            str(self.get_topological_view().shape)))


class BBCIPylearnCleanDataset(BBCIPylearnDataset):
    def __init__(self, filenames, cleaner, **kwargs):
        self.cleaner = cleaner
        super(BBCIPylearnCleanDataset, self).__init__(filenames, **kwargs)

    def load_bbci_set(self):  
        """ Only load clean data """
        # Data may be loaded already... e.g. if train set was loaded
        # and now valid set is being loaded as part of same set
        self.clean_and_load_set()
        log.info("Loaded clean data with shape {:s}.".format(
            self.bbci_set.epo.data.shape))           
    
    def clean_and_load_set(self):
        self.load_full_set()
        self.determine_clean_trials_and_chans()
        self.select_sensors()
        self.preproc_and_load_clean_trials()
    
    def load_full_set(self):
        log.info("Loading set...")
        # First load whole set
        self.bbci_set.load_continuous_signal()
        self.bbci_set.add_markers()
    
    def determine_clean_trials_and_chans(self):
        log.info("Cleaning set...")
        assert isinstance(self.filenames, basestring)
        filename = self.filenames # expecting only one filename
        (rejected_chans, rejected_trials, clean_trials) = self.cleaner.clean(
            self.bbci_set.cnt, filename)
        assert np.array_equal(np.union1d(clean_trials, rejected_trials),
            range(len(self.bbci_set.cnt.markers))), ("All trials should "
                "either be clean or rejected.")
        assert np.intersect1d(clean_trials, rejected_trials).size == 0, ("All "
            "trials should either be clean or rejected.")

        self.rejected_chans = rejected_chans
        self.rejected_trials = rejected_trials
        self.clean_trials = clean_trials # just for later info

    def select_sensors(self):
        if len(self.rejected_chans) > 0:
            self.bbci_set.cnt = select_channels(self.bbci_set.cnt, 
                self.rejected_chans, invert=True)
        if self.sensor_names is not None:
            self.bbci_set.cnt = select_channels(self.bbci_set.cnt, 
                self.sensor_names)
        cleaned_sensor_names = self.bbci_set.cnt.axes[-1]
        self.sensor_names = cleaned_sensor_names

    def preproc_and_load_clean_trials(self):
        log.info("Preprocessing set...")
        self.bbci_set.preprocess_continuous_signal()
        self.bbci_set.segment_into_trials()
        if len(self.rejected_trials) > 0:
            self.bbci_set.epo = select_epochs(self.bbci_set.epo, 
                self.rejected_trials, invert=True)
        # select epochs does not update marker structure...
        clean_markers = [m for i,m in enumerate(self.bbci_set.epo.markers) \
            if i not in self.rejected_trials]
        self.bbci_set.epo.markers = clean_markers
        self.bbci_set.remove_continuous_signal()
        self.bbci_set.preprocess_trials()

class BBCIPylearnCleanFFTDataset(BBCIPylearnCleanDataset):
    def __init__(self, filenames, transform_function_and_args,
        frequency_start=None, frequency_stop=None, **kwargs):
        self.frequency_start = frequency_start
        self.frequency_stop = frequency_stop
        self.transform_function_and_args = transform_function_and_args
        super(BBCIPylearnCleanFFTDataset, self).__init__(filenames, **kwargs)
        
    def load_bbci_set(self):
        self.clean_and_load_set()
        self.transform_with_fft()
            
    def transform_with_fft(self):
        # transpose as transform functions expect 
        # #trials x#channels x#samples order
        # (in wyrm framework it is #trials x#samples x#channels normally)
        trials = self.bbci_set.epo.data.transpose(0,2,1)
        fs = self.bbci_set.epo.fs
        win_length_ms, win_stride_ms = 500,250
        assert (win_length_ms == 500 and win_stride_ms == 250), ("if not, "
            "check if fft_pipeline function would still work")
        
        
        win_length = win_length_ms * fs / 1000
        win_stride = win_stride_ms * fs / 1000
        # Make sure that window length in ms is exactly fitting 
        # a certain number of samples
        assert (win_length_ms * fs) % 1000 == 0
        assert (win_stride_ms * fs) % 1000 == 0
        transform_func = self.transform_function_and_args[0]
        transform_kwargs = self.transform_function_and_args[1]
        # transform function will return 
        # #trials x #channels x#timebins x #freqboms
        transformed_trials = transform_func(trials, 
                window_length=win_length, 
                window_stride=win_stride, **transform_kwargs)
        self.bbci_set.epo.data = transformed_trials.transpose(0,2,1,3) # should be
        #transposed back in pylearnbbcidataset class

        # possibly select frequencies
        freq_bins = np.fft.rfftfreq(win_length, 1.0/self.bbci_set.epo.fs)
        if self.frequency_start is not None:
            freq_bin_start = freq_bins.tolist().index(self.frequency_start)
        else:
            freq_bin_start = 0
        if self.frequency_stop is not None:
            # + 1 as later indexing will exclude stop
            freq_bin_stop = freq_bins.tolist().index(self.frequency_stop) + 1
        else:
            freq_bin_stop = self.bbci_set.epo.data.shape[-1]
        self.bbci_set.epo.data = self.bbci_set.epo.data[:,:,:,
            freq_bin_start:freq_bin_stop]

def compute_power_and_phase(trials, window_length, window_stride,
        divide_win_length, square_amplitude, phases_diff):
    """Expects trials #trialsx#channelsx#samples order"""
    fft_trials = compute_short_time_fourier_transform(trials, 
        window_length=window_length, window_stride=window_stride)
    # now #trialsx#channelsx#samplesx#freqs
    # Todelay: Not sure if division by window length is necessary/correct?
    power_spectra = np.abs(fft_trials)
    if (square_amplitude):
        power_spectra = power_spectra ** 2
    if (divide_win_length):
        power_spectra = power_spectra / window_length
    phases = np.angle(fft_trials)
    if phases_diff:
        # Transform phases to "speed": diff of phase
        # minus diff of phase in timebin before for each
        # frequency bin separately
        phases =  phases[:, :, 1:, :] - phases[:, :, :-1, :]
        # pad a zero at the beginning to preserve dimensionality
        phases = np.pad(phases, ((0,0),(0,0),(1,0),(0,0)), 
            mode='constant', constant_values=0)
    power_and_phases = np.concatenate((power_spectra, phases), axis=1)
    return power_and_phases

def compute_power_spectra(trials, window_length, window_stride, 
        divide_win_length, square_amplitude):
    """Expects trials #trialsx#channelsx#samples order"""
    fft_trials = compute_short_time_fourier_transform(trials, 
        window_length=window_length, window_stride=window_stride)
    # Todelay: Not sure if division by window length is necessary/correct?
    # Hopefully does not matter since standardization will divide by variance
    # anyways
    power_spectra = np.abs(fft_trials)
    if (square_amplitude):
        power_spectra = power_spectra ** 2
    if (divide_win_length):
        power_spectra = power_spectra / window_length
    return power_spectra
    

def compute_short_time_fourier_transform(trials, window_length, window_stride):
    """Expects trials #trialsx#channelsx#samples order"""
    start_times = np.arange(0, trials.shape[2] - window_length+1, window_stride)
    freq_bins = int(np.floor(window_length / 2) + 1)
    fft_trials = np.empty((trials.shape[0], trials.shape[1], len(start_times), freq_bins), dtype=complex)
    for time_bin, start_time in enumerate(start_times):        
        w = blackmanharris(window_length)
        w=w/np.linalg.norm(w)
        trials_for_fft = trials[:,:,start_time:start_time+window_length] * w
        fft_trial = np.fft.rfft(trials_for_fft, axis=2)
        fft_trials[:,:,time_bin, :] = fft_trial
    return fft_trials

class BBCIPylearnCleanFilterbankDataset(BBCIPylearnCleanDataset):
    def __init__(self, filenames, min_freq, max_freq,
            last_low_freq, low_width, high_width,
            **kwargs):
        self.min_freq = min_freq
        self.max_freq = max_freq
        self.last_low_freq = last_low_freq
        self.low_width = low_width
        self.high_width = high_width
        super(BBCIPylearnCleanFilterbankDataset, self).__init__(filenames, **kwargs)
        
    def load_bbci_set(self):
        self.load_full_set()
        self.determine_clean_trials_and_chans()
        self.select_sensors()
        log.info("Preprocessing continuous signal...")
        self.bbci_set.preprocess_continuous_signal()
        self.create_filterbank()
        
    def create_filterbank(self):
        log.info("Creating filterbank...")
        # Create filterbands and array for holding
        # filterband trials 
        self.filterbands = generate_filterbank(self.min_freq, self.max_freq,
            self.last_low_freq, self.low_width, self.high_width)
        segment_length =  self.segment_ival[1] - self.segment_ival[0]
        num_samples = segment_length  * self.bbci_set.cnt.fs / 1000.0
        assert num_samples.is_integer()
        num_samples = int(num_samples)
        full_epo_data = np.empty((len(self.clean_trials), num_samples, 
            len(self.sensor_names), len(self.filterbands)), dtype=np.float32)
        # Fill filterbank
        self.fill_filterbank_data(full_epo_data)
        # Transform to wyrm epoched dataset
        clean_markers = [m for i,m in enumerate(self.bbci_set.cnt.markers) \
            if i not in self.rejected_trials]
        del self.bbci_set.cnt
        new_epo = Data(data=full_epo_data, 
            axes=self.filterband_axes, names=self.filterband_names,
            units=self.filterband_names)
        new_epo.markers = clean_markers
        self.bbci_set.epo = new_epo

    def fill_filterbank_data(self, full_epo_data):
        for filterband_i in xrange(len(self.filterbands)): 
            low_freq, high_freq= self.filterbands[filterband_i]
            log.info("Filterband {:d} of {:d}, from {:5.2f} to {:5.2f}".format(
                filterband_i + 1, len(self.filterbands), low_freq, high_freq))
            bandpassed_cnt = bandpass_cnt(self.bbci_set.cnt, 
                low_freq, high_freq, filt_order=3)
            epo = segment_dat_fast(bandpassed_cnt, 
                   marker_def={'1 - Right Hand': [1], '2 - Left Hand': [2], 
                       '3 - Rest': [3], '4 - Feet': [4]}, 
                   ival=self.segment_ival)
            epo.data = np.float32(epo.data)
            epo = select_epochs(epo, self.rejected_trials, invert=True)
            full_epo_data[:,:,:,filterband_i] = epo.data
            del epo.data
            del bandpassed_cnt
        self.filterband_axes = epo.axes + [self.filterbands.tolist()]
        self.filterband_names = epo.names + ['filterband']
        self.filterband_units = epo.units + ['Hz']
    
    def reload(self):
        log.info("Reloading...")
        if (hasattr(self, 'X')):
            del self.X
        self.load()

def format_targets(y):
    # matlab has one-based indexing and one-based labels
    # have to convert to zero-based labels so subtract 1...
    y = y - 1
    # we need only a 1d-array of integers
    # squeeze in case of 2 dimensions, make sure it is still 1d in case of
    # a single number (can happen for test runs with just one trial)
    y = np.atleast_1d(y.squeeze())
    y = y.astype(int)
    target_formatter = OneHotFormatter(4)
    y = target_formatter.format(y)
    return y

def adjust_start_stop(num_trials, given_start, given_stop):
    # allow to specify start trial as percentage of total dataset
    if isinstance(given_start, float):
        assert given_start >= 0 and given_start <= 1
        given_start = int(num_trials * given_start)
    if isinstance(given_stop, float):
        assert given_stop >= 0 and given_stop <= 1
        # use -1 to ensure that if stop given as e.g. 0.6
        # and next set uses start as 0.6 they are completely
        # seperate/non-overlapping
        given_stop = int(num_trials * given_stop) - 1
    # if start or stop not existing set to 0 and end of dataset :)
    start = given_start or 0
    stop = given_stop or num_trials
    return start, stop
