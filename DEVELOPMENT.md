# Testing

DoJobber tests are run via tox.

```
$ tox
```

It currently runs unit tests as well as

* flake8
* pylint
* black

Please keep pylint exceptions to a minimum and avoid global exceptions.


Code is formatted via
![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
with 80 column width. This is compatible with flake8

```
$ black -S -l 80 file.py
```

