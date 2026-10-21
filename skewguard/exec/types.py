"""Shared records for identifying matrix cells and recording observed outcomes.

"""

from enum import StrEnum

class Cell(StrEnum):
    A0D0 = "A0D0" # a - application, d - db. old app, old database
    A0D1 = "A0D1" # old app, new db
    A1D1 = "A1D1" # new app new db
    A0D1W = "A0D1w" # old code against migrated db after a new code write

