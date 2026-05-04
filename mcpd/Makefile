# MCPd - JSON-RPC daemon for AmigaOS 4.
#
# Cross-compile via the walkero/amigagccondocker:os4-gcc11 image
# (Docker Desktop on Windows). The `docker-build` target wraps the
# image; the bare targets (all / clean) are for use *inside* the
# container.

CC      ?= ppc-amigaos-gcc
CFLAGS  ?= -O2 -Wall -Wextra -std=gnu11 \
           -I../third-party/cjson
# zlib comes from z.library at runtime (option 2 in the size-cap
# planning report), opened in main.c. Static -lz is gone - smaller
# binary + zlib's internal allocator no longer goes through clib4.
LDFLAGS ?= -lauto

# Build date stamped into the $VER cookie (DD.MM.YYYY, Amiga Version format).
BUILD_DATE := $(shell date +%d.%m.%Y)
MCPD_CFLAGS = -DMCPD_DATE=\"$(BUILD_DATE)\"

# AmigaOS 4 SDK + toolchain identifier embedded into MCPd. Surfaced
# via `MCPd --version`, `proto.version`, and `proto.capabilities`
# build.sdk so users / CI can see exactly which SDK produced a given
# binary.
#
# Default tracks the walkero/amigagccondocker:os4-gcc11 image used
# by `make docker-build` and CI: AmigaOS 4.1 FE SDK 54.16 + GCC 11.5.
# Override at build time when targeting a different SDK, e.g.
#   make SDK_VERSION="AmigaOS 4.1 FE SDK 54.30 + GCC 13.4"
SDK_VERSION ?= AmigaOS 4.1 FE SDK 54.16 + GCC 11.5 (walkero/amigagccondocker:os4-gcc11)
MCPD_CFLAGS += -DMCPD_SDK_VERSION="\"$(SDK_VERSION)\""

# Distribution
VERSION = 1.1

SRC_DIR = src
METHODS_DIR = $(SRC_DIR)/methods
SRCS = \
  $(SRC_DIR)/main.c \
  $(SRC_DIR)/frame.c \
  $(SRC_DIR)/rpc.c \
  $(SRC_DIR)/b64.c \
  $(SRC_DIR)/sha256.c \
  $(SRC_DIR)/discovery.c \
  $(SRC_DIR)/crashhook.c \
  $(METHODS_DIR)/helpers.c \
  $(METHODS_DIR)/amigautil.c \
  $(METHODS_DIR)/proto.c \
  $(METHODS_DIR)/fs.c \
  $(METHODS_DIR)/exec.c \
  $(METHODS_DIR)/sys.c \
  $(METHODS_DIR)/wb.c \
  $(METHODS_DIR)/debug.c \
  $(METHODS_DIR)/events.c \
  $(METHODS_DIR)/hwres.c \
  $(METHODS_DIR)/elfm.c \
  $(METHODS_DIR)/applib.c \
  $(METHODS_DIR)/mcu.c \
  ../third-party/cjson/cJSON.c

# Docker Desktop image used for cross-compilation.
DOCKER       ?= docker
DOCKER_IMAGE ?= walkero/amigagccondocker:os4-gcc11

# When invoked from MSYS2 bash on Windows, `$(CURDIR)` is a Unix-style
# path (e.g. `/c/msys64/...`) which Docker Desktop rejects. `pwd -W`
# yields the equivalent Windows path. On Linux / macOS, plain `pwd`
# is what Docker expects.
#
# We mount the *project root* (one level up from mcpd/) at /src so
# `../third-party/cjson/...` resolves inside the container.
ifeq ($(OS),Windows_NT)
DOCKER_PROJECT_ROOT := $(shell cd .. && pwd -W)
DOCKER_ENV := MSYS_NO_PATHCONV=1
else
DOCKER_PROJECT_ROOT := $(realpath ..)
DOCKER_ENV :=
endif

.PHONY: all clean docker-build docker-shell

all: MCPd

MCPd: $(SRCS)
	$(CC) $(CFLAGS) $(MCPD_CFLAGS) -o $@ $(SRCS) $(LDFLAGS)

# Run `make all` inside the cross-compile container. Invoke from the
# *host* (Docker Desktop). Mounts the project root at /src so the
# Makefile's `../third-party/...` references resolve.
docker-build:
	$(DOCKER_ENV) $(DOCKER) run --rm -v "$(DOCKER_PROJECT_ROOT):/src" -w /src/mcpd $(DOCKER_IMAGE) make all

# Drop into an interactive shell in the build container.
docker-shell:
	$(DOCKER_ENV) $(DOCKER) run --rm -it -v "$(DOCKER_PROJECT_ROOT):/src" -w /src/mcpd $(DOCKER_IMAGE) bash

clean:
	rm -f MCPd MCPd.debug *.o
