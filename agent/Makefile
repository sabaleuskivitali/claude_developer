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

# Minimum exe size for a proper dotnet publish --self-contained build (bytes).
# dotnet build apphost is ~9 MB; dotnet publish single-file is ~190 MB.
# If the exe in OUT_DIR is below this threshold the build is wrong → fail fast.
MIN_EXE_MB := 100

.PHONY: all build pack validate deploy clean tag

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

## 2. Validate — fail if exe is too small (built with dotnet build, not publish)
validate:
	@EXE_SIZE=$$(stat -f%z $(OUT_DIR)/WinDiagSvc.exe 2>/dev/null || stat -c%s $(OUT_DIR)/WinDiagSvc.exe); \
	MIN_BYTES=$$(($(MIN_EXE_MB) * 1024 * 1024)); \
	if [ "$$EXE_SIZE" -lt "$$MIN_BYTES" ]; then \
		echo ""; \
		echo "ERROR: WinDiagSvc.exe is $$(du -sh $(OUT_DIR)/WinDiagSvc.exe | cut -f1) — too small!"; \
		echo "       This was built with 'dotnet build', NOT 'dotnet publish'."; \
		echo "       Run 'make build' to produce a correct self-contained single-file exe."; \
		echo ""; \
		exit 1; \
	fi
	@echo "    OK: exe size check passed ($$(du -sh $(OUT_DIR)/WinDiagSvc.exe | cut -f1) >= $(MIN_EXE_MB)MB)"

## 3. Pack — zip: exe + installer files + browser extension
pack: validate
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
	@rm -f $(ZIP_PATH)
	@cd /tmp/agent-zip && zip -qj $(ZIP_PATH) WinDiagSvc.exe install.ps1 nssm.exe appsettings.json WinDiagUpdater.ps1 native-messaging-host.json \
		&& zip -qr $(ZIP_PATH) extension/
	@echo "    OK: $(ZIP_PATH) ($$(du -sh $(ZIP_PATH) | cut -f1))"

## 3. Deploy — upload zip to cloud server
deploy: all
	@echo "==> Deploying to $(CLOUD_HOST):$(CLOUD_PATH)..."
	@ssh $(CLOUD_HOST) 'mkdir -p ~/cloud/app/agent'
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
