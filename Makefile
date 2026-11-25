# Phase 10. Everything runs in the aero-pipeline image, which drives the host's Podman
# (OpenFOAM stays in its own pinned image). `make demo-batch RUN=` runs on the host instead
# (needs the .venv set up as in CLAUDE.md, activated).
IMAGE ?= localhost/aero-pipeline:0.1
SOCK  ?= $(XDG_RUNTIME_DIR)/podman/podman.sock
RUN   ?= podman run --rm --userns=keep-id --security-opt label=disable -e HOME=/tmp \
         -v $(CURDIR):$(CURDIR) -w $(CURDIR) -v $(SOCK):/run/podman/podman.sock $(IMAGE)

.PHONY: image socket doctor test demo-baseline demo-batch

image:            ## build the Python-side image (needs vendor/OpenVSP-3.57.0-Ubuntu-26.04_amd64.deb)
	podman build -t $(IMAGE) -f Containerfile .

socket:           ## start the host's rootless Podman API socket (once per login)
	systemctl --user enable --now podman.socket

doctor: socket    ## check container engine, OpenFOAM image, OpenVSP, paths
	$(RUN) aero doctor

test:             ## unit tests (no CFD)
	$(RUN) pytest -q --basetemp=$(CURDIR)/.pytest_tmp  # temp cases must be visible to the host engine

demo-baseline: socket  ## 10 variants, narrow ranges, alpha 0/4: every run should pass (~1 h)
	$(RUN) sh -c 'aero init-study configs/study_demo_baseline.yaml && aero run demo-baseline && aero summary demo-baseline --reports'

demo-batch: socket     ## 10 variants, wide ranges, alpha 6/10: some runs flagged for review (~1.5-2 h)
	$(RUN) sh -c 'aero init-study configs/study_demo_batch.yaml && aero run demo-batch && aero summary demo-batch --reports'
