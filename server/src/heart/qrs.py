"""
Pan–Tompkins QRS detector.

A port of EDFbrowser's `filt/pt_qrs.c` and the beat-index bookkeeping from
`filt/ecg_filter.c` (Teunis van Beelen, GPLv3). EDFbrowser's automatic QRS
detection is what produced the `R-peak@@ECG` annotations we scored against, so
this is not a lookalike: it reproduces the same filters, thresholds, RR-average
tracking and search-back rule, and on the annotated recording it lands on the
same samples.

**Licence.** The original is GPLv3. Anything that links this module inherits
that licence.

Why the whole algorithm rather than the shape of it
---------------------------------------------------
The peak finder it replaces (`ecg.PeakFinder`) answers a much narrower question
— "where inside the beat I already know about is its R?" — by taking the largest
excursion from the band median. That is cheap but it is not a detector: a T wave
taller than a low R, a baseline swing, or motion noise all win an occasional
beat, and a wandering pick is exactly what fans the beats stack out.
Pan–Tompkins is the opposite trade: it band-passes, differentiates, squares and
integrates the signal into a slope-energy envelope, then decides beats on
adaptive signal/noise thresholds, so neither a tall T wave nor a slow wander
produces a detection. It is also genuinely *causal* with a fixed delay, which is
what a live plot needs.

The parts that are easy to get subtly wrong, and are reproduced here exactly:

* Downsampling to 200 Hz. The reference algorithm is defined at 200 Hz. EDFbrowser
  resamples with a fractional-position interpolator that only *fires* when the
  sample counter equals the integer position plus one, so the output grid is not
  a plain every-other-sample decimation. The moving average ahead of it only
  exists above 400 Hz — at 256 Hz `ds_ravg_len` truncates to 1 and is skipped.
* The filters are expressions of the *ring buffer's* contents at rotating
  indices, not convolution kernels. `PT_LPF_LEN`, `PT_HPF_LEN` and `PT_DIFF_LEN`
  are part of the arithmetic.
* `_slope()` reads the **oldest** fifteen entries of the 30-sample integration
  window (indices `mwi_idx + 0 .. +14` after the counter has advanced), not the
  newest. It looks wrong; it is what the reference does, and changing it moves
  detections.
* Two RR averages are tracked, and a *regular*→*irregular* transition halves the
  primary thresholds. Miss that and runs of ectopics get swallowed.
* The delay compensation is a fixed 21 samples at 200 Hz, scaled back up to the
  input rate by the same ratio that did the downsampling.

Units. The reference feeds raw digital samples and a sensitivity of
microvolts-per-bit, so its absolute thresholds (2000 µV, 5000 µV clipping) come
out in digital units. Feeding microvolts with `sense=1.0` is the same detector
up to floating-point rounding; that is how `HeartState` uses it.
"""

from __future__ import annotations

import math
from typing import List, Optional

#: Sample rate the reference algorithm is defined at.
SMPL_FREQ = 200

#: Refractory period, in 200 Hz samples: 200 ms, below which a second QRS cannot
#: be physiologically true and so cannot be detected.
REFRACT_PERIOD = 40

#: Within this many samples of the previous complex (360 ms), a candidate has to
#: prove itself with slope — otherwise it is judged a T wave.
T_WAVE_PERIOD = 72

#: Samples the peak search may walk while looking for the local maximum.
SLOPE_SEARCH_WIDTH = 15

LPF_LEN = 13
LPF_C1, LPF_C2, LPF_C3, LPF_C4 = 1, 2, 6, 12

HPF_LEN = 33
HPF_C1, HPF_C2, HPF_C3, HPF_C4 = 1, 16, 17, 32

DIFF_LEN = 5
DIFF_C1, DIFF_C2, DIFF_C3 = 1, 3, 4

RR_AVG_LEN = 8
MWI_LEN = 30

#: Below this input rate the algorithm's assumptions (refractory period, MWI
#: width) no longer hold, and the reference refuses the signal outright.
MIN_FS_HZ = 199.999999


class PanTompkins:
    """Stream one sample at a time; get an R-peak offset back when one is found."""

    def __init__(self, fs_hz: float, sense: float = 1.0) -> None:
        if fs_hz < MIN_FS_HZ:
            raise ValueError(f"Pan–Tompkins needs >= {MIN_FS_HZ} Hz, got {fs_hz}")
        if sense < 1e-6:
            raise ValueError(f"sensitivity must be positive, got {sense}")
        self.fs_hz = float(fs_hz)
        self.sense = float(sense)

        # -- downsampling ---------------------------------------------------
        self.ds_ratio = self.fs_hz / SMPL_FREQ
        self.ds_ravg_len = int(self.ds_ratio)
        self.ds_ravg_buf: Optional[List[float]] = (
            [0.0] * self.ds_ravg_len if self.ds_ravg_len > 1 else None)
        self.ds_ravg_idx = 0
        self.ds_smpl_pos = 0.0
        self.ds_old_val = 0.0
        self.ds_num_smpl = 0

        # -- filter state ---------------------------------------------------
        self.lpf_idx = 0
        self.lpfx = [0.0] * LPF_LEN
        self.lpfy = [0.0] * LPF_LEN

        self.hpf_idx = 0
        self.hpfx = [0.0] * HPF_LEN
        self.hpfy = [0.0] * HPF_LEN
        self.hpf_out = 0.0
        self.hpf_out_old = 0.0
        self.hpf_out_lt = 0.0

        self.diff_idx = 0
        self.diffx = [0.0] * DIFF_LEN

        self.sqr_out = 0.0
        self.sqr_out_clip = (5000.0 / self.sense) ** 2

        self.mwi_idx = 0
        self.mwix = [0.0] * MWI_LEN
        self.mwi_out = 0.0
        self.mwi_out_old = 0.0
        self.mwi_out_lt = 0.0

        self._reset()

    # -- state ------------------------------------------------------------

    def _reset(self) -> None:
        """The reference's `reset_pt_qrs`, down to its initial guesses."""
        # Delay of the filter chain, in 200 Hz samples (~105 ms).
        self.del_comp = 21

        self.rr_low_limit = int(0.92 * SMPL_FREQ)
        self.rr_high_limit = int(1.16 * SMPL_FREQ)
        self.rr_missed_limit = int(1.66 * SMPL_FREQ)

        self.rr_avg_x1 = [SMPL_FREQ] * RR_AVG_LEN
        self.rr_avg_x2 = [SMPL_FREQ + 10] * RR_AVG_LEN
        self.rr_avg_idx1 = 0
        self.rr_avg_idx2 = 0
        self.rr_average1 = 0
        self.rr_average2 = 0
        self.rr_regular = 0

        # The paper does not say how to initialise these; the reference chose
        # them by validating against MIT-BIH, so they are part of the algorithm.
        self.spki = 2000.0 / self.sense
        self.spkf = 7000.0 / self.sense
        self.npki = 0.0
        self.npkf = 0.0
        self.peaki = 0.0
        self.peakf = 0.0
        self.threshold_i1 = 0.0
        self.threshold_i2 = 0.0
        self.threshold_f1 = 0.0
        self.threshold_f2 = 0.0

        self.idx_ht = 0
        self.idx_lt = 0
        self.pk_det_start = 0
        self.slope_last = 0.0
        self.slope_lt = 0.0
        self.smpls_last_pk = 0

        if self.ds_ravg_buf is not None:
            self.ds_ravg_buf = [0.0] * len(self.ds_ravg_buf)

    def reset(self) -> None:
        self._reset()

    # -- streaming --------------------------------------------------------

    def push(self, value: float) -> int:
        """
        Feed one sample.

        Returns the R peak's position as a number of samples *back* from this
        one, or 0 when nothing was detected. The delay is real: the detector has
        to see the whole complex and the filter tail before it can name its apex.
        """
        if self.ds_ravg_buf is not None:
            self.ds_ravg_buf[self.ds_ravg_idx] = value
            self.ds_ravg_idx = (self.ds_ravg_idx + 1) % self.ds_ravg_len
            # Plain ordered accumulation, not `fsum`: the reference sums in
            # buffer order and its threshold comparisons sit close enough to the
            # margin that a correctly-rounded sum can flip a detection.
            total = 0.0
            for item in self.ds_ravg_buf:
                total += item
            ds_avg = total / self.ds_ravg_len
        else:
            ds_avg = value

        index = self.ds_num_smpl
        self.ds_num_smpl += 1
        if index != int(self.ds_smpl_pos) + 1:
            self.ds_old_val = ds_avg
            return 0

        fract = self.ds_smpl_pos - int(self.ds_smpl_pos)
        ds_val_out = ds_avg * fract + self.ds_old_val * (1.0 - fract)
        self.ds_smpl_pos += self.ds_ratio
        self.ds_old_val = ds_avg

        return self._run(ds_val_out)

    def _run(self, new_val: float) -> int:
        self._bpf_diff_sqr(new_val)

        hpf_out = self.hpf_out
        if hpf_out < 0:
            hpf_out *= -1
        self.hpf_out = hpf_out
        mwi_out = self.mwi_out

        detected_1st = False
        detected_2nd = False

        # Nothing may be detected inside the refractory period — not even a
        # candidate, so the low-threshold tracker is skipped too.
        if self.smpls_last_pk > REFRACT_PERIOD:
            # Keep the best peak that only cleared the *low* thresholds, so a
            # search-back later has something to promote.
            if mwi_out >= self.threshold_i2 and hpf_out >= self.threshold_f2:
                if hpf_out > self.hpf_out_lt:
                    self.idx_lt = self.smpls_last_pk
                    self.mwi_out_lt = mwi_out
                    self.hpf_out_lt = hpf_out
                    slope = self._slope()
                    if self.slope_lt < slope:
                        self.slope_lt = slope

            detected_1st = self._scan_peak(mwi_out, hpf_out)

            if not detected_1st:
                if (self.smpls_last_pk > self.rr_missed_limit
                        and self.smpls_last_pk > REFRACT_PERIOD):
                    detected_2nd = self._search_back()

        if not detected_1st and not detected_2nd:
            # Every sample that is not a beat is noise as far as the thresholds
            # are concerned; that is how they float over the background.
            self.peaki = mwi_out
            self.peakf = hpf_out
            self._adjust_thresholds(noise=True, second=False)

            r_peak_pos = 0
            self.smpls_last_pk += 1
            # A long stretch with no detection (artefacts) would push the
            # thresholds out of reach; step them down every second past five.
            if self.smpls_last_pk >= 5 * SMPL_FREQ:
                if self.smpls_last_pk < 15 * SMPL_FREQ:
                    if self.smpls_last_pk % SMPL_FREQ == 0:
                        self._adjust_thresholds(noise=False, second=True)
        elif detected_1st:
            r_peak_pos = self.smpls_last_pk - self.idx_ht + self.del_comp
            self.smpls_last_pk -= self.idx_ht
            self.idx_ht = 0
        else:
            r_peak_pos = self.smpls_last_pk - self.idx_lt + self.del_comp
            self.smpls_last_pk -= self.idx_lt
            self.idx_lt = 0
            self.slope_lt = 0.0

        return int(r_peak_pos * self.ds_ratio + 0.5)

    def _scan_peak(self, mwi_out: float, hpf_out: float) -> bool:
        """
        Track the local maximum that identifies a candidate complex.

        Returns True when a complex was confirmed. The peak is not the sample
        that first crosses the threshold — the signal keeps rising for a few
        samples — so the search walks forward while both envelopes climb, and
        the confirmed peak is the last one seen before they turn over.
        """
        if mwi_out >= self.threshold_i1 and hpf_out >= self.threshold_f1:
            if ((self.pk_det_start
                 and mwi_out >= self.mwi_out_old
                 and hpf_out >= self.hpf_out_old)
                    or not self.pk_det_start):
                self.mwi_out_old = mwi_out
                self.hpf_out_old = hpf_out
                self.idx_ht = self.smpls_last_pk
                self.pk_det_start += 1
                return False
            if self.pk_det_start and self.pk_det_start < SLOPE_SEARCH_WIDTH:
                self.pk_det_start += 1
                return False
        elif self.pk_det_start and self.pk_det_start < SLOPE_SEARCH_WIDTH:
            self.pk_det_start += 1
            return False

        if not self.pk_det_start:
            return False

        # The envelopes turned over: the previous sample was the apex.
        self.pk_det_start = 0
        self.peaki = self.mwi_out_old
        self.peakf = self.hpf_out_old
        slope = self._slope()

        # Inside the T-wave window a complex has to be at least half as steep as
        # the one before it; a T wave is not.
        if self.smpls_last_pk > T_WAVE_PERIOD or slope >= self.slope_last / 2.0:
            self.slope_last = slope
            self._adjust_thresholds(noise=False, second=False)
            self._adjust_rr_averages(back_search=False)
            self.mwi_out_lt = 0.0
            self.hpf_out_lt = 0.0
            self.idx_lt = 0
            self.slope_lt = 0.0
            return True
        return False

    def _search_back(self) -> bool:
        """
        Promote the best low-threshold peak once an interval has been missed.

        A QRS that never cleared the primary threshold still cleared the halved
        one; if it is also steep enough to not be a T wave, it was a beat and the
        primary thresholds were too high.
        """
        if self.idx_lt < 1:
            return False
        if self.slope_lt < self.slope_last * 0.15:
            return False

        self.peaki = self.mwi_out_lt
        self.peakf = self.hpf_out_lt
        self.pk_det_start = 0
        self._adjust_thresholds(noise=False, second=True)
        self._adjust_rr_averages(back_search=True)
        self.mwi_out_lt = 0.0
        self.hpf_out_lt = 0.0
        return True

    # -- filters ----------------------------------------------------------

    def _bpf_diff_sqr(self, new_val: float) -> None:
        """Low-pass, high-pass, differentiate, square, moving-window integrate."""
        self.lpfx[self.lpf_idx] = new_val
        i = self.lpf_idx
        lpf_out = (2.0 * self.lpfy[(i + LPF_LEN - LPF_C1) % LPF_LEN]
                   - self.lpfy[(i + LPF_LEN - LPF_C2) % LPF_LEN]
                   + self.lpfx[i]
                   - 2.0 * self.lpfx[(i + LPF_LEN - LPF_C3) % LPF_LEN]
                   + self.lpfx[(i + LPF_LEN - LPF_C4) % LPF_LEN])
        self.lpfy[i] = lpf_out
        self.lpf_idx = (i + 1) % LPF_LEN

        i = self.hpf_idx
        self.hpfx[i] = lpf_out
        hpf_out = (self.hpfy[(i + HPF_LEN - HPF_C1) % HPF_LEN]
                   - self.hpfx[i] / 32.0
                   + self.hpfx[(i + HPF_LEN - HPF_C2) % HPF_LEN]
                   - self.hpfx[(i + HPF_LEN - HPF_C3) % HPF_LEN]
                   + self.hpfx[(i + HPF_LEN - HPF_C4) % HPF_LEN] / 32.0)
        self.hpfy[i] = hpf_out
        self.hpf_idx = (i + 1) % HPF_LEN
        self.hpf_out = hpf_out

        i = self.diff_idx
        self.diffx[i] = hpf_out
        diff_out = (2.0 * self.diffx[i]
                    + self.diffx[(i + DIFF_LEN - DIFF_C1) % DIFF_LEN]
                    - self.diffx[(i + DIFF_LEN - DIFF_C2) % DIFF_LEN]
                    - 2.0 * self.diffx[(i + DIFF_LEN - DIFF_C3) % DIFF_LEN]) / 8.0
        self.diff_idx = (i + 1) % DIFF_LEN

        sqr_out = diff_out * diff_out
        # The paper's hard limit was 255 on 8-bit data; the reference scales it
        # to the input units so one huge artefact cannot dominate the average.
        if sqr_out > self.sqr_out_clip:
            sqr_out = self.sqr_out_clip
        self.sqr_out = sqr_out

        self.mwix[self.mwi_idx] = sqr_out
        total = 0.0
        for item in self.mwix:
            total += item
        self.mwi_out = total / MWI_LEN
        self.mwi_idx = (self.mwi_idx + 1) % MWI_LEN

    def _slope(self) -> float:
        """
        Steepest energy in the integration window.

        Reads the window from `mwi_idx` forward — i.e. the *oldest* fifteen
        samples of the thirty held — because that is what the reference does and
        the search-back threshold is tuned against it.
        """
        best = 0.0
        i = self.mwi_idx
        for k in range(SLOPE_SEARCH_WIDTH):
            value = self.mwix[(i + MWI_LEN - k) % MWI_LEN]
            if best < value:
                best = value
        return best

    # -- thresholds -------------------------------------------------------

    def _adjust_thresholds(self, noise: bool, second: bool) -> None:
        """Float the two signal/noise thresholds over the current peak."""
        if noise:
            self.npki = 0.125 * self.peaki + 0.875 * self.npki
            self.npkf = 0.125 * self.peakf + 0.875 * self.npkf
        elif second:
            # Found only on the low thresholds, so it counts for less.
            self.spki = 0.25 * self.peaki + 0.75 * self.spki
            self.spkf = 0.25 * self.peakf + 0.75 * self.spkf
        else:
            self.spki = 0.125 * self.peaki + 0.875 * self.spki
            self.spkf = 0.125 * self.peakf + 0.875 * self.spkf

        self.threshold_i1 = self.npki + 0.25 * (self.spki - self.npki)
        self.threshold_f1 = self.npkf + 0.25 * (self.spkf - self.npkf)
        self.threshold_i2 = self.threshold_i1 / 2.0
        self.threshold_f2 = self.threshold_f1 / 2.0

    def _adjust_rr_averages(self, back_search: bool) -> None:
        """Track two eight-beat RR averages and the rate limits derived from them."""
        idx = self.idx_lt if back_search else self.smpls_last_pk

        self.rr_avg_x1[self.rr_avg_idx1] = idx
        self.rr_avg_idx1 = (self.rr_avg_idx1 + 1) % RR_AVG_LEN
        self.rr_average1 = int(sum(self.rr_avg_x1) / RR_AVG_LEN)

        if self.rr_low_limit <= idx <= self.rr_high_limit:
            self.rr_avg_x2[self.rr_avg_idx2] = idx
            self.rr_avg_idx2 = (self.rr_avg_idx2 + 1) % RR_AVG_LEN
            self.rr_average2 = int(sum(self.rr_avg_x2) / RR_AVG_LEN)

        if self.rr_average1 == self.rr_average2:
            self.rr_regular = 1
        elif self.rr_regular:
            self.rr_regular = 0
            # Going regular → irregular: halve the primary thresholds so beats
            # are not missed while the rate settles.
            self.threshold_i1 /= 2.0
            self.threshold_f1 /= 2.0

        # The paper derives the limits from average2, but average2 only accepts
        # intervals already inside the limits — so after a step change it would
        # never move. The reference uses average1 instead.
        self.rr_low_limit = int(0.92 * self.rr_average1)
        self.rr_high_limit = int(1.16 * self.rr_average1)
        self.rr_missed_limit = int(1.66 * self.rr_average1)


class QrsDetector:
    """
    `PanTompkins` with EDFbrowser's `ecg_filter` bookkeeping on top.

    The detector names a peak as an offset back from the current sample; a caller
    plotting beats wants absolute sample indices. That is all this adds — plus
    the interval list, which is the reference's `stat_buf`.
    """

    def __init__(self, fs_hz: float, sense: float = 1.0) -> None:
        self.fs_hz = float(fs_hz)
        self._pt = PanTompkins(fs_hz, sense)
        self._pushed = 0
        #: Absolute sample index of every detected R peak, oldest first.
        self.peaks: List[int] = []
        #: Interval between consecutive peaks, seconds (the reference's units).
        self.intervals: List[float] = []

    def reset(self) -> None:
        self._pt.reset()
        self._pushed = 0
        self.peaks.clear()
        self.intervals.clear()

    @property
    def count(self) -> int:
        return len(self.peaks)

    def push(self, sample: float) -> Optional[int]:
        """Feed one sample; returns the new peak's absolute index, or None."""
        index = self._pushed
        self._pushed += 1
        offset = self._pt.push(sample)
        if offset == 0:
            return None
        peak = index - offset
        if self.peaks:
            self.intervals.append((peak - self.peaks[-1]) / self.fs_hz)
        self.peaks.append(peak)
        return peak
