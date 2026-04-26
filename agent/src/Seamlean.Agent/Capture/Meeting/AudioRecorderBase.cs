using Concentus.Enums;
using Concentus.Oggfile;
using Concentus.Structs;
using NAudio.Wave;

namespace Seamlean.Agent.Capture.Meeting;

/// <summary>
/// Captures audio from an IWaveIn device, resamples to 16kHz mono 16-bit,
/// and streams Opus OGG to a temp file. Call StartAsync() / StopAsync().
/// </summary>
internal abstract class AudioRecorderBase : IAsyncDisposable
{
    private const int TargetSampleRate = 16000;
    private const int TargetChannels   = 1;
    private const int TargetBitDepth   = 16;
    private const int FrameSamples     = 960; // 60ms at 16kHz

    private IWaveIn?                _capture;
    private BufferedWaveProvider?   _buffer;
    private MediaFoundationResampler? _resampler;
    private FileStream?             _fileStream;
    private OpusOggWriteStream?     _oggStream;
    private Task?                   _consumerTask;
    private CancellationTokenSource _cts = new();

    public string? OutputPath { get; private set; }

    protected abstract IWaveIn CreateCapture();

    public Task StartAsync(string outputPath)
    {
        if (_capture is not null)
            throw new InvalidOperationException("Already recording");

        OutputPath  = outputPath;
        Directory.CreateDirectory(Path.GetDirectoryName(outputPath)!);

        _capture = CreateCapture();
        _buffer  = new BufferedWaveProvider(_capture.WaveFormat)
        {
            BufferDuration    = TimeSpan.FromSeconds(20),
            DiscardOnBufferOverflow = true,
        };

        var targetFormat = new WaveFormat(TargetSampleRate, TargetBitDepth, TargetChannels);
        _resampler  = new MediaFoundationResampler(_buffer, targetFormat) { ResamplerQuality = 60 };

        _fileStream = File.OpenWrite(outputPath);
        var encoder = new OpusEncoder(TargetSampleRate, TargetChannels, OpusApplication.OPUS_APPLICATION_VOIP);
        encoder.Bitrate = 32_000;
        _oggStream = new OpusOggWriteStream(encoder, _fileStream, null, TargetSampleRate);

        _cts = new CancellationTokenSource();
        _consumerTask = Task.Run(() => ConsumeLoop(_cts.Token));

        _capture.DataAvailable += OnData;
        _capture.StartRecording();
        return Task.CompletedTask;
    }

    public async Task<string?> StopAsync()
    {
        if (_capture is null) return null;

        _capture.DataAvailable -= OnData;
        _capture.StopRecording();

        // Let the consumer drain the remaining buffer
        await Task.Delay(500);
        _cts.Cancel();
        try { await (_consumerTask ?? Task.CompletedTask); } catch { }

        try { _oggStream?.Finish(); } catch { }
        _fileStream?.Dispose();

        _resampler?.Dispose();
        _capture?.Dispose();
        _capture = null;

        return OutputPath;
    }

    private void OnData(object? _, WaveInEventArgs e)
        => _buffer?.AddSamples(e.Buffer, 0, e.BytesRecorded);

    private void ConsumeLoop(CancellationToken ct)
    {
        if (_resampler is null || _oggStream is null) return;

        var readBuf  = new byte[FrameSamples * TargetChannels * (TargetBitDepth / 8)];
        var shortBuf = new short[FrameSamples * TargetChannels];

        while (!ct.IsCancellationRequested || (_buffer?.BufferedBytes ?? 0) > 0)
        {
            if (_buffer?.BufferedBytes < readBuf.Length)
            {
                Thread.Sleep(10);
                continue;
            }

            int read = _resampler.Read(readBuf, 0, readBuf.Length);
            if (read < readBuf.Length) continue;

            Buffer.BlockCopy(readBuf, 0, shortBuf, 0, read);
            try { _oggStream.WriteSamples(shortBuf, 0, FrameSamples); }
            catch { break; }
        }
    }

    public async ValueTask DisposeAsync()
    {
        await StopAsync();
        _cts.Dispose();
    }
}
