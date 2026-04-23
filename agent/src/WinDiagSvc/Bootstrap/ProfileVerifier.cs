using System.Security.Cryptography;

namespace WinDiagSvc.Bootstrap;

public static class ProfileVerifier
{
    // ECDSA-P256 public key of the bootstrap CA.
    // Replace with output of: python manage.py bootstrap export-pubkey
    // This is the ONLY root of trust for bootstrap profiles — never fetch this key at runtime.
    private const string CaPublicKeyPem = """
        -----BEGIN PUBLIC KEY-----
        REPLACE_WITH_SERVER_CA_PUBLIC_KEY_PEM
        -----END PUBLIC KEY-----
        """;

    public static bool Verify(SignedBootstrapProfile signed)
    {
        if (string.IsNullOrEmpty(signed.SignedData) || string.IsNullOrEmpty(signed.Signature))
            return false;

        if (CaPublicKeyPem.Contains("REPLACE_WITH_SERVER_CA_PUBLIC_KEY_PEM"))
            throw new InvalidOperationException(
                "Bootstrap CA public key not configured. " +
                "Run 'python manage.py bootstrap export-pubkey' on the server, " +
                "replace the placeholder in ProfileVerifier.cs, and rebuild.");

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
