import os, gc, time, runpy, argparse
from threading import RLock
from functools import partial
from math import floor, ceil
from typing import Callable, Dict, Iterable, List, Optional

import vapoursynth as vs
core = vs.core
core.add_cache = False

#import matplotlib as mpl
#mpl.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.figure import figaspect


__all__ = ['descale_cropping_args']


def get_descaler(kernel: str, b: int = 0, c: float = 1 / 2, taps: int = 3) -> Callable[..., vs.VideoNode]:
    if kernel == 'bilinear':
        return core.descale.Debilinear
    elif kernel == 'bicubic':
        return partial(core.descale.Debicubic, b=b, c=c)
    elif kernel == 'lanczos':
        return partial(core.descale.Delanczos, taps=taps)
    elif kernel == 'spline16':
        return core.descale.Despline16
    elif kernel == 'spline36':
        return core.descale.Despline36
    elif kernel == 'spline64':
        return core.descale.Despline64
    else:
        raise ValueError('_get_descaler: invalid kernel specified.')


def get_scaler(kernel: str, b: int = 0, c: float = 1 / 2, taps: int = 3) -> Callable[..., vs.VideoNode]:
    if kernel == 'bilinear':
        return core.resize.Bilinear
    elif kernel == 'bicubic':
        return partial(core.resize.Bicubic, filter_param_a=b, filter_param_b=c)
    elif kernel == 'lanczos':
        return partial(core.resize.Lanczos, filter_param_a=taps)
    elif kernel == 'spline16':
        return core.resize.Spline16
    elif kernel == 'spline36':
        return core.resize.Spline36
    elif kernel == 'spline64':
        return core.resize.Spline64
    else:
        raise ValueError('_get_scaler: invalid kernel specified.')


# https://github.com/Infiziert90/getnative/blob/c4bfbb07165db315e3c5d89e68f294892b2effaf/getnative/utils.py#L27
def vpy_source_filter(path: os.PathLike) -> vs.VideoNode:
    runpy.run_path(path, {}, '__vapoursynth__')
    return vs.get_output(0)


# https://github.com/stuxcrystal/vapoursynth/blob/9ce5fe890dfc6f60ccedd096f1c5ecef64fe52fb/src/cython/vapoursynth.pyx#L1548
# basically, self -> clip
def frames(clip: vs.VideoNode, prefetch: Optional[int] = None, backlog: Optional[int] = None) -> Iterable[vs.VideoFrame]:
    if prefetch is None or prefetch <= 0:
        prefetch = vs.core.num_threads
    if backlog is None or backlog < 0:
        backlog = prefetch * 3
    elif backlog < prefetch:
        backlog = prefetch
    
    enum_fut = enumerate(clip.get_frame_async(frameno) for frameno in range(len(clip)))

    finished = False
    running = 0
    lock = RLock()
    reorder = {}

    def _request_next():
        nonlocal finished, running
        with lock:
            if finished:
                return

            ni = next(enum_fut, None)
            if ni is None:
                finished = True
                return

            running += 1

            idx, fut = ni
            reorder[idx] = fut
            fut.add_done_callback(_finished)

    def _finished(f):
        nonlocal finished, running
        with lock:
            running -= 1
            if finished:
                return

            if f.exception() is not None:
                finished = True
                return

            _refill()

    def _refill():
        if finished:
            return
        
        with lock:
            # Two rules: 1. Don't exceed the concurrency barrier
            #            2. Don't exceed unused-frames-backlog
            while (not finished) and (running < prefetch) and len(reorder) < backlog:
                _request_next()

    _refill()

    sidx = 0
    try:
        while (not finished) or (len(reorder) > 0) or running > 0:
            if sidx not in reorder:
                # Spin. Reorder being empty should never happen.
                continue
        
            # Get next requested frame
            fut = reorder[sidx]

            result = fut.result()
            del reorder[sidx]
            _refill()

            sidx += 1
            yield result
    
    finally:
        finished = True
        gc.collect()


# https://github.com/Infiziert90/getnative/blob/c4bfbb07165db315e3c5d89e68f294892b2effaf/getnative/utils.py#L64
def to_float(str_value: str) -> float:
    if set(str_value) - set("0123456789./"):
        raise argparse.ArgumentTypeError("Invalid characters in float parameter")
    try:
        return eval(str_value) if "/" in str_value else float(str_value)
    except (SyntaxError, ZeroDivisionError, TypeError, ValueError):
        raise argparse.ArgumentTypeError("Exception while parsing float") from None


def getw(clip: vs.VideoNode, height: int) -> int:
    ''' Only force even result if height is even.
    '''
    width = ceil(height * clip.width / clip.height)
    if height % 2 == 0:
        width = width // 2 * 2
    return width


def descale_cropping_args(clip: vs.VideoNode, src_height: float, base_height: int, base_width: Optional[int] = None, mode: str = 'wh') -> Dict:
    assert base_height >= src_height
    if base_width is None:
        base_width = getw(clip, base_height)
    src_width = src_height * clip.width / clip.height
    cropped_width = base_width - 2 * floor((base_width - src_width) / 2)
    cropped_height = base_height - 2 * floor((base_height - src_height) / 2)
    args = dict()
    args_w = dict(
        width = cropped_width,
        src_width = src_width,
        src_left = (cropped_width - src_width) / 2
    )
    args_h = dict(
        height = cropped_height,
        src_height = src_height,
        src_top = (cropped_height - src_height) / 2
    )
    if 'w' in mode.lower():
        args.update(args_w)
    if 'h' in mode.lower():
        args.update(args_h)
    return args


def gen_descale_error(clip: vs.VideoNode, frame_no: int, base_height: int, base_width: int, src_heights: List[float], kernel: str = 'bicubic', b: int = 0, c: float = 1 / 2, taps: int = 3, mode: str = 'wh', thr: float = 0.015, show_plot: bool = True, save_path: Optional[os.PathLike] = None) -> None:
    num_samples = len(src_heights)
    clips = clip[frame_no].resize.Point(format=vs.GRAYS, matrix_s='709' if clip.format.color_family == vs.RGB else None).std.Cache() * num_samples
    # Descale
    descaler = get_descaler(kernel, b, c, taps)
    scaler = get_scaler(kernel, b, c, taps)
    def _rescale(n, clip):
        cropping_args = descale_cropping_args(clip, src_heights[n], base_height, base_width, mode)
        descaled = descaler(clip, **cropping_args)
        cropping_args.update(width=clip.width, height=clip.height)
        return scaler(descaled, **cropping_args)
    rescaled = core.std.FrameEval(clip, partial(_rescale, clip=clips))
    diff = core.std.Expr([clips, rescaled], f'x y - abs dup {thr} > swap 0 ?').std.Crop(10, 10, 10, 10).std.PlaneStats().std.Cache()
    # Collect error
    errors = [0.0] * num_samples
    for n, f in enumerate(frames(diff)):
        print(f'\r{n}/{num_samples}', end='')
        errors[n] = f.props['PlaneStatsAverage']
    print('\n')
    # Plot
    p = plt.figure()
    plt.close('all')
    plt.style.use('dark_background')
    _, ax = plt.subplots(figsize=figaspect(1/2))
    ax.plot(src_heights, errors, '.w-', linewidth=1)
    ax.set(xlabel='src_height', ylabel='Error', yscale='log')
    if save_path is not None:
        plt.savefig(save_path)
    if show_plot:
        plt.show()
    plt.close(p)


def main() -> None:
    parser = argparse.ArgumentParser(description='Find the native fractional resolution of upscaled material (mostly anime)')
    parser.add_argument('--frame', '-f', dest='frame_no', type=int, default=0, help='Specify a frame for the analysis, default is 0')
    parser.add_argument('--kernel', '-k', dest='kernel', type=str.lower, default='bicubic', help='Resize kernel to be used')
    parser.add_argument('--bicubic-b', '-b', dest='b', type=to_float, default='0', help='B parameter of bicubic resize')
    parser.add_argument('--bicubic-c', '-c', dest='c', type=to_float, default='1/2', help='C parameter of bicubic resize')
    parser.add_argument('--lanczos-taps', '-t', dest='taps', type=int, default=3, help='Taps parameter of lanczos resize')
    parser.add_argument('--base-height', '-bh', dest='bh', type=int, default=None, help='Base integer height before cropping')
    parser.add_argument('--base-width', '-bw', dest='bw', type=int, default=None, help='Base integer width before cropping')
    parser.add_argument('--min-src-height', '-min', dest='sh_min', type=to_float, default=720, help='Minimum native height of src_height to consider')
    parser.add_argument('--step-length', '-sl', dest='sh_step', type=to_float, default='0.25', help='Step length of src_height searching')
    parser.add_argument('--threshold', '-thr', dest='thr', type=to_float, default='0.015', help='Threshold for calculating descaling error')
    parser.add_argument('--mode', '-m', dest='mode', type=str.lower, default='wh', help='Mode for descaling, options are wh (default), w (descale in width only) and h (descale in height only)')
    parser.add_argument('--save-dir', '-dir', dest='save_dir', type=str, default=None, help='Location of output error plot directory')
    parser.add_argument('--save-ext', '-ext', dest='save_ext', type=str, default='svg', help='File extension of output error plot file')
    parser.add_argument(dest='input_file', type=str, help='Absolute or relative path to the input VPY script')
    args = parser.parse_args()
    ext = os.path.splitext(args.input_file)[1]
    assert ext.lower() in {'.py', '.pyw', '.vpy'}
    clip = vpy_source_filter(args.input_file)

    if args.save_dir is None:
        dir_out = os.path.join(os.path.dirname(args.input_file), 'getfnative_results')
        os.makedirs(dir_out, exist_ok=True)
    else:
        dir_out = args.save_dir
    save_path = dir_out + os.path.sep + f'getfnative-f{args.frame_no}-bh{args.bh}'
    n = 1
    while True:
        if os.path.exists(save_path + f'-{n}.' + args.save_ext):
            n = n + 1
            continue
        else:
            save_path = save_path + f'-{n}.' + args.save_ext
            break

    starttime = time.time()

    assert args.sh_step > 0.0 and args.sh_min < args.bh - args.sh_step
    max_samples = floor((args.bh - args.sh_min) / args.sh_step) + 1
    src_heights = [args.sh_min + n * args.sh_step for n in range(max_samples)]
    if args.bw is None:
        args.bw = getw(clip, args.bh)
        print(f'Using base width {args.bw}.')
    gen_descale_error(clip, args.frame_no, args.bh, args.bw, src_heights, args.kernel, args.b, args.c, args.taps, args.mode, args.thr, True, save_path)

    print(f'Done in {time.time() - starttime:.2f}s')


if __name__ == '__main__':
    main()

