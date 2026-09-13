# insteonrf — C helpers for the SDR receive path.
#
#   make           build fsk2_demod and rf_clip
#   make test      build, then run the Python test suite
#   make lint      ruff + mypy (needs the dev extra installed)
#   make clean     remove objects; make realclean also removes the binaries
#
# The Python side needs no build step: 'pip install -e .' installs insteon-rf.
# There is no fsk2_mod target any more — 'insteon-rf modulate' (numpy) replaced
# the never-committed liquid-dsp modulator.

UNAME = $(shell uname)
CC ?= gcc

MKDIR_P = mkdir -p

OBJECTS_DIR = Obj
SOURCE_DIR = Src
CFLAGS += -ggdb -O2 -Wall -Wextra -Werror
LDFLAGS += -ggdb
TESTDAT = Dat/41802513110D2711018C00.dat

PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

all: directories fsk2_demod rf_clip

.PHONY: all directories test lint check clean realclean p

directories: $(OBJECTS_DIR)

test: fsk2_demod
	$(PYTHON) -m pytest -q

lint:
	$(PYTHON) -m ruff check .
	$(PYTHON) -m mypy

check: lint test

# Decode the reference capture — the line the README and tests assert.
fixture: fsk2_demod
	./fsk2_demod -U < $(TESTDAT) | $(PYTHON) -m insteonrf.cli print

fsk2_demod: $(OBJECTS_DIR)/fsk2_demod.o $(OBJECTS_DIR)/fxpt_atan2.o
	$(CC) $(LDFLAGS) -O2 -pipe $+ -o $@ -lm

rf_clip: $(OBJECTS_DIR)/rf_clip.o
	$(CC) $(LDFLAGS) -O2 -pipe $< -o $@

$(OBJECTS_DIR)/%.o: $(SOURCE_DIR)/%.c
	$(CC) -c $(CFLAGS) -o $@ $<

$(OBJECTS_DIR):
	@$(MKDIR_P) $(OBJECTS_DIR)

p:
	@echo UNAME $(UNAME)
	@echo CC $(CC)
	@echo PYTHON $(PYTHON)

clean:
	@/bin/rm -rf $(OBJECTS_DIR)

realclean: clean
	@/bin/rm -f ./rf_clip ./fsk2_demod
