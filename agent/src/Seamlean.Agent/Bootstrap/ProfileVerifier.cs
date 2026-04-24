using System.Security.Cryptography;

namespace Seamlean.Agent.Bootstrap;

public static class ProfileVerifier
{
    // ECDSA-P256 public key of the Seamlean cloud CA.
    // Profiles are re-signed by api.seamlean.com — server CA key is irrelevant.
    // To rotate: generate new cloud CA key, update this constant, release new agent version.
    private const string CaPublicKeyPem = """
        -----BEGIN PUBLIC KEY-----
        MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAENzfF/esu6mgwZo5hQiUyzFi9ZLt0
        L4wl24KEBakkgz9xHiNr2HYoQ3JMCRAdlDqxoqz+O5RMPVGey2fPpvICCQ==
        -----END PUBLIC KEY-----
        """;

    public static bool Verify(SignedBootstrapProfile signed)
    {
        if (string.IsNullOrEmpty(signed.SignedData) || string.IsNullOrEmpty(signed.Signature))
            return false;

        try
        {
            var data = Convert.FromBase64String(signed.SignedData);
            var sig  = Convert.FromBase64String(signed.Signature);

            using var ecdsa = ECDsa.Create();
            ecdsa.ImportFromPem(CaPublicKeyPem.AsSpan());

            // DER-encoded signature from Python cryptography library
            return ecdsa.VerifyData(
                data, sig,
                HashAlgorithmName.SHA256,
                DSASignatureFormat.Rfc3279DerSequence);
        }
        catch
        {
            return false;
        }
    }
}
