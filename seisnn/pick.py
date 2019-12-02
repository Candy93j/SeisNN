import os
import shutil

import scipy
import numpy as np
import tensorflow as tf
import tensorflow_probability as tfp
from obspy import read
from obspy.core.event.base import QuantityError, WaveformStreamID
from obspy.core.event.origin import Pick
from scipy.signal import find_peaks
from tqdm import tqdm


def get_pick_list(events):
    pick_list = []
    for event in events:
        for p in event.picks:
            pick_list.append(p)
    pick_list.sort(key=lambda pick: pick.time)
    return pick_list


def get_window(pick, trace_length=30):
    scipy.random.seed()
    pick_time = pick.time

    starttime = pick_time - trace_length + np.random.random_sample() * trace_length
    endtime = starttime + trace_length

    window = {
        'starttime': starttime,
        'endtime': endtime,
        'station': pick.waveform_id.station_code,
        'wavename': pick.waveform_id.wavename
    }
    return window


def get_pdf(stream, sigma=0.1):
    trace = stream[0]
    tfd = tfp.distributions
    start_time = trace.stats.starttime
    x_time = trace.times(reftime=start_time)
    stream.pdf = {}
    for phase, picks in stream.picks.items():
        phase_pdf = np.zeros((len(x_time),))

        for pick in picks:
            pick_time = pick.time - start_time
            dist = tfd.Normal(loc=pick_time, scale=sigma)
            pick_pdf = dist.prob(x_time)

            if tf.math.reduce_max(pick_pdf):
                phase_pdf += pick_pdf / tf.math.reduce_max(pick_pdf)

        if tf.math.reduce_max(phase_pdf):
            phase_pdf = phase_pdf / tf.math.reduce_max(phase_pdf)

        stream.pdf[phase] = phase_pdf.numpy()
    return stream


def get_picks_from_pdf(trace, height=0.5, distance=100):
    start_time = trace.stats.starttime
    peaks, properties = find_peaks(trace.pdf, height=height, distance=distance)

    picks = []
    for p in peaks:
        if p:
            time = start_time + p / trace.stats.sampling_rate
            phase_hint = "P"
            pick = Pick(time=time, phase_hint=phase_hint)
            pick.waveform_id = WaveformStreamID(network_code=trace.stats.network, station_code=trace.stats.station,
                                                location_code=trace.stats.channel, channel_code=trace.stats.location)
            picks.append(pick)

    return picks


def get_picks_from_dataset(dataset):
    pick_list = []
    trace = read(dataset, headonly=True).traces[0]
    picks = trace.picks
    pick_list.extend(picks)
    return pick_list


def search_pick(pick_list, stream):
    tmp_pick = {}
    starttime = stream.traces[0].stats.starttime
    endtime = stream.traces[0].stats.endtime
    for pick in pick_list:
        phase = pick.phase_hint
        if starttime < pick.time < endtime:
            if not tmp_pick.get(phase):
                tmp_pick[phase] = [pick]
            else:
                tmp_pick[phase].append(pick)

    return tmp_pick


def get_exist_picks(stream, pick_list):
    picks = search_pick(pick_list, stream)
    stream.picks = picks
    return stream


def write_pdf_to_dataset(predict, dataset_list, dataset_output_dir, remove_dir=False):
    if remove_dir:
        shutil.rmtree(dataset_output_dir, ignore_errors=True)
    os.makedirs(dataset_output_dir, exist_ok=True)

    print("Output file:")
    with tqdm(total=len(dataset_list)) as pbar:
        for i, prob in enumerate(predict):
            try:
                trace = read(dataset_list[i]).traces[0]

            except IndexError:
                break

            trace_length = trace.data.size
            pdf = prob.reshape(trace_length, )

            if pdf.max():
                trace.pdf = pdf / pdf.max()
            else:
                trace.pdf = pdf
            pdf_picks = get_picks_from_pdf(trace)

            if trace.picks:
                for val_pick in trace.picks:
                    for pre_pick in pdf_picks:
                        pre_pick.evaluation_mode = "automatic"

                        residual = get_time_residual(val_pick, pre_pick)
                        pre_pick.time_errors = QuantityError(residual)

                        if is_close_pick(val_pick, pre_pick, delta=0.1):
                            pre_pick.evaluation_status = "confirmed"
                        elif is_close_pick(val_pick, pre_pick, delta=1):
                            pre_pick.evaluation_status = "rejected"

            else:
                trace.picks = []
                for pre_pick in pdf_picks:
                    pre_pick.evaluation_mode = "automatic"

            trace.picks.extend(pdf_picks)
            time_stamp = trace.stats.starttime.isoformat()
            trace.write(dataset_output_dir + '/' + time_stamp + trace.get_id() + ".pkl", format="PICKLE")
            pbar.update()


def is_close_pick(validate_pick, predict_pick, delta=0.1):
    pick_upper_bound = predict_pick.time + delta
    pick_lower_bound = predict_pick.time - delta
    if pick_lower_bound < validate_pick.time < pick_upper_bound:
        return True
    else:
        return False


def get_time_residual(val_pick, pre_pick):
    if is_close_pick(val_pick, pre_pick, delta=0.5):
        residual = val_pick.time - pre_pick.time
        return residual
