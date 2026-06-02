# -*- coding: utf-8 -*-

name = "l_repo_sync_gui"
version = "999.0"
description = "GUI tool to upload/download each rez-package-source package repo"
authors = ["Lugwit Team"]

requires = [
    "python-3.12+<3.13",
    "pyside6",
    "l_qt_wgt_lib",
    "pytracemp",
    "gitpython",
]


def commands():
    env.PYTHONPATH.prepend("{root}/src")
    alias("l_repo_sync_gui", "python {root}/src/l_repo_sync_gui/main.py")


build_command = False
cachable = True
relocatable = True
