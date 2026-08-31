import sys

from hf_downloader.app import run


if __name__ == "__main__":
    if "--worker" in sys.argv:
        sys.argv.remove("--worker")
        from hf_downloader.worker import main as worker_main

        raise SystemExit(worker_main())
    run()
