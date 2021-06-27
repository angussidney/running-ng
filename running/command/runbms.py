import logging
from typing import Dict, List
from running.benchmark import JavaBenchmarkSuite, JavaProgram
from running.config import Configuration
from pathlib import Path
import socket
from datetime import datetime
from running.jvm import JVM
from running.modifier import JVMArg
import tempfile
import subprocess
import os

configuration: Configuration


def setup_parser(subparsers):
    f = subparsers.add_parser("runbms")
    f.set_defaults(which="runbms")
    f.add_argument("LOG_DIR", type=Path)
    f.add_argument("CONFIG", type=Path)
    f.add_argument("N", type=int)
    f.add_argument("n", type=int, nargs='+')
    f.add_argument("-i", "--invocations", type=int)


def getid() -> str:
    host = socket.gethostname()
    now = datetime.now()
    timestamp = now.strftime("%Y-%m-%d-%a-%H%M%S")
    return "{}-{}".format(host, timestamp)


def spread(spread_factor: int, N: int, n: int) -> float:
    """Spread the numbers

    For example, when we have N = 8, n = 0, 1, ..., 7, N,
    we can have fractions of form n / N with equal distances between them:
    0/8, 1/8, 2/8, ..., 7/8, 8/8

    Sometimes it's desirable to transform such sequence with finer steppings
    at the start than at the end.
    The idea is that when the values are small, even small changes can greatly
    affect the behaviour of a system (such as the heap sizes of GC), so we want
    to explore smaller values at a finer granularity.

    For each n, we define
    n' = n + spread_factor / (N - 1) * (1 + 2 + ... + n-2 + n-1)
    For a consecutive pair (n-1, n), we can see that they are additionally
    (n-1) / (N-1) * spread_factor apart.
    For a special case when n = N, we have N' = N + N/2 * spread_factor,
    and (N'-1, N') is spread_factor apart.

    Let's concretely use spread_factor = 1 as as example.
    Let N and n be the same.
    So we have spread_factor / (N - 1) = 1/7
    n = 0, n' = 0,
    n = 1, n' = 1.
    n = 2, n' = 2 + 1/7
    n = 3, n' = 3 + 1/7 + 2/7 = 3 + 3/7
    ...
    n = 7, n' = 7 + 1/7 + 2/7 + ... + 6/7 = 7 + 21/7 = 10
    n = 8, n' = 8 + 1/7 + 2/7 + ... + 7/7 = 8 + 28/7 = 12

    Parameters
    ----------
    N : int
        Denominator
    spread_factor : int
        How much coarser is the spacing at the end relative to start
        spread_factor = 0 doesn't change the sequence at all
    n: int
        Nominator
    """
    sum_1_n_minus_1 = (n*n - n) / 2
    return n + spread_factor / (N - 1) * sum_1_n_minus_1


def hfac_str(hfac: float) -> str:
    return str(int(hfac*1000))


def get_heapsize(hfac: float, minheap: int) -> int:
    return round(minheap * hfac)


def get_hfacs(heap_range: int, spread_factor: int, N: int, ns: List[int]) -> List[float]:
    start = 1.0
    end = float(heap_range)
    divisor = spread(spread_factor, N, N)/(end-start)
    return [spread(spread_factor, N, n)/divisor + start for n in ns]


def run_benchmark_with_config(c: str, b: JavaProgram, timeout: int, fd) -> str:
    jvm = configuration.get("jvms")[c.split('|')[0]]
    mods = [configuration.get("modifiers")[x]
            for x in c.split('|')[1:]]
    mod_b = b.attach_modifiers(mods)
    prologue = get_log_prologue(jvm, mod_b)
    fd.write(prologue)
    output = mod_b.run(jvm, timeout)
    fd.write(output)
    epilogue = get_log_epilogue(jvm, mod_b)
    fd.write(epilogue)
    return output


def get_filename(bm: JavaProgram, hfac: float, size: int, config: str) -> str:
    return "{}.{}.{}.{}.log".format(
        bm.name,
        hfac_str(hfac),
        size,
        ".".join(config.split("|"))
    )


def get_log_epilogue(jvm: JVM, bm: JavaProgram) -> str:
    return ""


def system(cmd) -> str:
    return subprocess.check_output(cmd, shell=True).decode("utf-8")


def hz_to_ghz(hzstr: str) -> str:
    return "{:.2f} GHz".format(int(hzstr) / 1000 / 1000)


def get_log_prologue(jvm: JVM, bm: JavaProgram) -> str:
    output = "\n-----\n"
    output += bm.to_string(jvm.get_executable())
    output += "\n"
    output += "Environment variables: \n"
    for k, v in sorted(os.environ.items()):
        output += "\t{}={}\n".format(k, v)
    output += "OS: "
    output += system("uname -a")
    output += "CPU: "
    output += system("cat /proc/cpuinfo | grep 'model name' | head -1")
    output += "number of cores: "
    cores = system("cat /proc/cpuinfo | grep MHz | wc -l")
    output += cores
    for i in range(0, int(cores)):
        output += "Frequency of cpu {}: ".format(i)
        output += hz_to_ghz(
            system("cat /sys/devices/system/cpu/cpu{}/cpufreq/scaling_cur_freq".format(i)))
        output += "\n"
        output += "Governor of cpu {}: ".format(i)
        output += system("cat /sys/devices/system/cpu/cpu{}/cpufreq/scaling_governor".format(i))
        output += "Scaling_min_freq of cpu {}: ".format(i)
        output += hz_to_ghz(
            system("cat /sys/devices/system/cpu/cpu{}/cpufreq/scaling_min_freq".format(i)))
        output += "\n"
    return output


def run_one_benchmark(
    invocations: int,
    suite: JavaBenchmarkSuite,
    bm: JavaProgram,
    hfac: float,
    configs: List[str],
    runbms_dir: Path,
    log_dir: Path
):
    bm_name = bm.bm_name
    print(bm_name, end=" ")
    print(hfac_str(hfac), end=" ")
    size = get_heapsize(hfac, suite.get_minheap(bm_name))
    print(size, end=" ")
    timeout = suite.get_timeout(bm_name)
    size_str = "{}M".format(size)
    heapsize = JVMArg(
        name="heap{}".format(size_str),
        val="-Xms{} -Xmx{}".format(size_str, size_str)
    )
    bm_with_heapsize = bm.attach_modifiers([heapsize])
    for i in range(0, invocations):
        print(i, end="", flush=True)
        for j, c in enumerate(configs):
            log_filename = get_filename(bm, hfac, size, c)
            logging.debug("Running with log filename {}".format(log_filename))
            with (log_dir / log_filename).open("a") as fd:
                output = run_benchmark_with_config(
                    c, bm_with_heapsize, timeout, fd
                )
                if "PASSED" in output:
                    print(chr(ord('a')+j), end="", flush=True)
                else:
                    print(".", end="", flush=True)
    for j, c in enumerate(configs):
        log_filename = get_filename(bm, hfac, size, c)
        subprocess.check_call("gzip {}".format(
            log_dir / log_filename), shell=True)
    print()


def run_one_hfac(
    invocations: int,
    hfac: float,
    suites: Dict[str, JavaBenchmarkSuite],
    benchmarks: Dict[str, List[JavaProgram]],
    configs: List[str],
    runbms_dir: Path,
    log_dir: Path
):
    for suite_name, bms in benchmarks.items():
        suite = suites[suite_name]
        for bm in bms:
            run_one_benchmark(invocations, suite, bm, hfac,
                              configs, runbms_dir, log_dir)


def run(args):
    with tempfile.TemporaryDirectory(prefix="runbms-") as runbms_dir:
        logging.info("Temporary directory: {}".format(runbms_dir))
        if args.get("which") != "runbms":
            return False
        global configuration
        configuration = Configuration.from_file(args.get("CONFIG"))
        configuration.resolve_class()
        id = getid()
        print("Run id: {}".format(id))
        log_dir = args.get("LOG_DIR") / id
        log_dir.mkdir(parents=True, exist_ok=True)
        N = args.get("N")
        ns = args.get("n")
        invocations = configuration.get("invocations")
        if args.get("invocations"):
            invocations = args.get("invocations")
        heap_range = configuration.get("heaprange")
        spread_factor = configuration.get("spreadfactor")
        suites = configuration.get("suites")
        benchmarks = configuration.get("benchmarks")
        configs = configuration.get("configs")
        hfacs = get_hfacs(heap_range, spread_factor, N, ns)
        logging.info("hfacs: {}".format(
            ", ".join([
                hfac_str(hfac)
                for hfac in hfacs
            ])
        ))
        for hfac in hfacs:
            run_one_hfac(invocations, hfac, suites, benchmarks,
                         configs, runbms_dir, log_dir)
            print()

        return True
