# Seamlean Agent — build, pack, deploy
# Usage:
#   make          → build + pack
#   make deploy   → build + pack + upload to cloud server
#   make clean    → remove build artifacts

DOTNET     := $(HOME)/.dotnet/dotnet
PROJ       := src/WinDiagSvc/WinDiagSvc.csproj
VERSION    := $(shell cat VERSION)
OUT_DIR    := /tmp/agent-publish
ZIP_PATH   := /tmp/WinDiagSvc.zip
CLOUD_HOST := agent@178.104.1.6
CLOUD_PATH := ~/cloud/app/agent/WinDiagSvc.zip

# ── Targets ─────────────────────────────────────────────────────────────────

.PHONY: all build pack deploy clean

all: build pack

## 1. Compile — dotnet publish (PublishSingleFile, SelfContained, win-x64)
build:
	@echo "==> Building agent v$(VERSION) (dotnet publish)..."
	@rm -rf $(OUT_DIR)
	$(DOTNET) publish $(PROJ) \
		-c Release \
		-r win-x64 \
		--self-contained \
		-p:PublishSingleFile=true \
		-p:IncludeNativeLibrariesForSelfExtract=true \
		-o $(OUT_DIR)
	@echo "    OK: $(OUT_DIR)/WinDiagSvc.exe ($$(du -sh $(OUT_DIR)/WinDiagSvc.exe | cut -f1))"

## 2. Pack — zip: exe + installer files + browser extension
pack:
	@echo "==> Packing zip..."
	@mkdir -p /tmp/agent-zip
	@cp $(OUT_DIR)/WinDiagSvc.exe        /tmp/agent-zip/
	@cp installer/install.ps1            /tmp/agent-zip/
	@cp installer/nssm.exe               /tmp/agent-zip/
	@cp installer/appsettings.json       /tmp/agent-zip/
	@cp installer/WinDiagUpdater.ps1     /tmp/agent-zip/
	@cp installer/native-messaging-host.json /tmp/agent-zip/
	@# Browser extension folder
	@rm -rf /tmp/agent-zip/extension
	@cp -r browser-extension /tmp/agent-zip/extension
	@cd /tmp/agent-zip && zip -qj $(ZIP_PATH) . \
		&& cd /tmp/agent-zip && zip -qr $(ZIP_PATH) extension/
	@echo "    OK: $(ZIP_PATH) ($$(du -sh $(ZIP_PATH) | cut -f1))"

## 3. Deploy — upload zip to cloud server
deploy: all
	@echo "==> Deploying to $(CLOUD_HOST):$(CLOUD_PATH)..."
	@ssh $(CLOUD_HOST) "mkdir -p $$(dirname $(CLOUD_PATH))"
	@scp $(ZIP_PATH) $(CLOUD_HOST):$(CLOUD_PATH)
	@echo "    OK: deployed agent v$(VERSION)"

## Tag release in git (call after commit)
tag:
	@echo "==> Tagging agent/v$(VERSION)..."
	@git tag -a "agent/v$(VERSION)" -m "agent v$(VERSION)"
	@echo "    OK: agent/v$(VERSION) — push with: git push origin agent/v$(VERSION)"

clean:
	@rm -rf $(OUT_DIR) /tmp/agent-zip $(ZIP_PATH)
	@echo "    OK: cleaned"
