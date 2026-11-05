from pathlib import Path

from setuptools import find_packages, setup

requirements = (Path(__file__).parent / "piezalink" / "requirements.txt").read_text(
    encoding="utf-8"
).splitlines()

setup(
    name="piezalink",
    version="0.1.0",
    description="Rovira catalog synchronization CLI",
    python_requires=">=3.11",
    packages=find_packages(),
    package_data={"piezalink": ["src/logo.txt", "requirements.txt"], "piezalink.core": ["schema.sql"]},
    install_requires=[line.strip() for line in requirements if line.strip() and not line.lstrip().startswith("#")],
    entry_points={"console_scripts": ["piezalink = piezalink.__main__:main"]},
)
