using NAudio.Wave;

namespace Seamlean.Agent.Capture.Meeting;

/// <summary>Records system audio output (loopback).</summary>
internal sealed class LoopbackRecorder : AudioRecorderBase
{
    protected override IWaveIn CreateCapture()
        => new WasapiLoopbackCapture();
}
