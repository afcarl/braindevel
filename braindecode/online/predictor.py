class OnlinePredictor(object):
    """ Online predictor accepts samples and supplies predictions.
    
    It uses the data processor to preprocess the data and the model
    to make the actual predictions. Online predictor is mainly responsible
    for cutting out correct time windows for the model to predict on. """
    
    def __init__(self, data_processor, model, pred_freq):
        self.data_processor = data_processor
        self.model = model
        self.pred_freq = pred_freq
        
    def initialize(self, n_chans):
        self.data_processor.initialize(n_chans)
        self.n_samples = 0
        self.i_last_pred = -1
        self.last_pred = None
        self.model.initialize()
        self.n_samples_pred_window = self.model.get_n_samples_pred_window()
        
    def receive_samples(self, samples):
        """Expect samples in timexchan format"""
        self.data_processor.process_samples(samples)
        self.n_samples += len(samples)
        if self.should_do_next_prediction():
            self.predict()
            
    def should_do_next_prediction(self):
        return (self.n_samples >= self.n_samples_pred_window and 
            self.n_samples > (self.i_last_pred + self.pred_freq))
    
    def predict(self):
        # Compute how many samples we already have past the
        # sample we wanted to predict
        # keep in mind: n_samples = n_samples (number of samples)
        n_samples_after_pred = min(self.n_samples - 
            self.n_samples_pred_window,
            self.n_samples - self.i_last_pred - self.pred_freq - 1)
        assert n_samples_after_pred < self.pred_freq, ("Other case "
            "not implemented yet")
        start = -self.n_samples_pred_window - n_samples_after_pred
        end = -n_samples_after_pred
        if end == 0:
            end = None
        topo = self.data_processor.get_samples(start, end)
        self.last_pred = self.model.predict(topo)
        # -1 since we have 0-based indexing in python
        self.i_last_pred = self.n_samples - n_samples_after_pred - 1
    
    def pop_last_prediction_and_sample_ind(self):
        last_pred = self.last_pred
        self.last_pred = None
        return last_pred, self.i_last_pred

    def has_new_prediction(self):
        return self.last_pred is not None
             
            
        