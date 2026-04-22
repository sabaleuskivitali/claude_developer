using System.Net;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using WinDiagSvc.Models;

namespace WinDiagSvc.Browser;

/// <summary>
/// Serves extension.crx and update_manifest.xml on http://localhost:9876/
/// so Chrome/Edge can force-install the extension on non-domain machines.
///
/// Chrome blocks file:/// update URLs on non-enterprise machines (Chrome 73+),
/// but http://localhost is served from the agent process itself and is not blocked.
///
/// Registry entry written by installer:
///   {ExtensionId};http://localhost:9876/update_manifest.xml
/// </summary>
public sealed class ExtensionHostService : BackgroundService
{
    private readonly AgentSettings _settings;
    private readonly ILogger<ExtensionHostService> _logger;

    public ExtensionHostService(
        IOptions<AgentSettings> options,
        ILogger<ExtensionHostService> logger)
    {
        _settings = options.Value;
        _logger   = logger;
    }

    private static string ReadExtensionVersion(string installDir)
    {
        try
        {
            var path = Path.Combine(installDir, "extension", "manifest.json");
            if (!File.Exists(path)) return "1.0.0";
            using var doc = System.Text.Json.JsonDocument.Parse(File.ReadAllText(path));
            if (doc.RootElement.TryGetProperty("version", out var v))
                return v.GetString() ?? "1.0.0";
        }
        catch { }
        return "1.0.0";
    }

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        var installDir = Path.GetDirectoryName(Environment.ProcessPath)!;
        var crxPath    = Path.Combine(installDir, "extension.crx");
        var extId      = _settings.ExtensionId;
        var port       = _settings.ExtensionHostPort;

        if (!File.Exists(crxPath))
        {
            _logger.LogWarning("ExtensionHostService: extension.crx not found at {Path}, skipping", crxPath);
            return;
        }

        if (string.IsNullOrEmpty(extId))
        {
            _logger.LogWarning("ExtensionHostService: ExtensionId not configured, skipping");
            return;
        }

        using var listener = new HttpListener();
        listener.Prefixes.Add($"http://localhost:{port}/");

        try
        {
            listener.Start();
        }
        catch (Exception ex)
        {
            _logger.LogWarning("ExtensionHostService: cannot bind to localhost:{Port} - {Msg}", port, ex.Message);
            return;
        }

        _logger.LogInformation("ExtensionHostService: serving extension on http://localhost:{Port}/", port);

        var extVersion = ReadExtensionVersion(installDir);
        _logger.LogInformation("ExtensionHostService: extension version {Ver}", extVersion);

        while (!ct.IsCancellationRequested)
        {
            HttpListenerContext ctx;
            try
            {
                ctx = await listener.GetContextAsync().WaitAsync(ct);
            }
            catch (OperationCanceledException) { break; }
            catch (Exception ex)
            {
                _logger.LogDebug("ExtensionHostService: listener error: {Msg}", ex.Message);
                continue;
            }

            _ = Task.Run(() => HandleRequest(ctx, crxPath, extId, port, extVersion), CancellationToken.None);
        }

        try { listener.Stop(); } catch { /* ignore */ }
    }

    private void HandleRequest(HttpListenerContext ctx, string crxPath, string extId, int port, string extVersion)
    {
        try
        {
            var path = ctx.Request.Url?.AbsolutePath ?? "/";
            var res  = ctx.Response;

            if (path == "/update_manifest.xml")
            {
                var xml = $"""
                    <?xml version='1.0' encoding='UTF-8'?>
                    <gupdate xmlns='http://www.google.com/update2/response' protocol='2.0'>
                      <app appid='{extId}'>
                        <updatecheck codebase='http://localhost:{port}/extension.crx' version='{extVersion}' />
                      </app>
                    </gupdate>
                    """;
                var bytes = System.Text.Encoding.UTF8.GetBytes(xml);
                res.ContentType     = "application/xml; charset=utf-8";
                res.ContentLength64 = bytes.Length;
                res.OutputStream.Write(bytes);
                _logger.LogDebug("ExtensionHostService: served update_manifest.xml");
            }
            else if (path == "/extension.crx")
            {
                var bytes = File.ReadAllBytes(crxPath);
                res.ContentType     = "application/x-chrome-extension";
                res.ContentLength64 = bytes.Length;
                res.OutputStream.Write(bytes);
                _logger.LogInformation("ExtensionHostService: served extension.crx ({Kb} KB)", bytes.Length / 1024);
            }
            else
            {
                res.StatusCode = 404;
            }

            res.Close();
        }
        catch (Exception ex)
        {
            _logger.LogDebug("ExtensionHostService: request handler error: {Msg}", ex.Message);
            try { ctx.Response.Abort(); } catch { /* ignore */ }
        }
    }
}
