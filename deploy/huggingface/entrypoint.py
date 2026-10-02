"""Root entry point copied into the Space deployment bundle as app.py."""
import runpy

if __name__ == "__main__":
    runpy.run_module("deploy.huggingface.app", run_name="__main__")
