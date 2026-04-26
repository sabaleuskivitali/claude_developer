using NAudio.CoreAudioApi;
using NAudio.Wave;

namespace Seamlean.Agent.Capture.Meeting;

/// <summary>Records from the default system microphone (WASAPI shared mode).</summary>
internal sealed class MicRecorder : AudioRecorderBase
{
    protected override IWaveIn CreateCapture()
        => new WasapiCapture(WasapiCapture.GetDefaultCaptureDevice(), true, 100)
        {
            ShareMode = AudioClientShareMode.Shared,
        };
}
