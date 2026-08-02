from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("dcs-linux-installer")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"
