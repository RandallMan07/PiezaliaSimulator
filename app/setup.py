from setuptools import find_packages, setup

setup(
    name="piezalink",
    version="0.1.0",
    description="Rovira catalog synchronization CLI",
    python_requires=">=3.11",
    packages=find_packages(),
    package_data={"piezalink": ["src/logo.txt"]},
    entry_points={"console_scripts": ["piezalink = piezalink.__main__:main"]},
)
