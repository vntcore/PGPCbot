import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.OutputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Base64;
import java.util.Collections;
import java.util.Date;
import java.util.Iterator;
import java.util.List;
import java.util.Locale;

import org.bouncycastle.openpgp.PGPKeyRing;
import org.bouncycastle.openpgp.PGPPublicKey;
import org.bouncycastle.openpgp.PGPPublicKeyRing;
import org.bouncycastle.openpgp.PGPSecretKeyRing;
import org.bouncycastle.util.encoders.Hex;
import org.bouncycastle.util.io.Streams;
import org.pgpainless.PGPainless;
import org.pgpainless.algorithm.CompressionAlgorithm;
import org.pgpainless.algorithm.DocumentSignatureType;
import org.pgpainless.algorithm.EncryptionPurpose;
import org.pgpainless.decryption_verification.ConsumerOptions;
import org.pgpainless.decryption_verification.DecryptionStream;
import org.pgpainless.decryption_verification.MessageMetadata;
import org.pgpainless.decryption_verification.SignatureVerification;
import org.pgpainless.encryption_signing.EncryptionOptions;
import org.pgpainless.encryption_signing.EncryptionResult;
import org.pgpainless.encryption_signing.EncryptionStream;
import org.pgpainless.encryption_signing.ProducerOptions;
import org.pgpainless.encryption_signing.SigningOptions;
import org.pgpainless.key.OpenPgpFingerprint;
import org.pgpainless.key.SubkeyIdentifier;
import org.pgpainless.key.generation.type.rsa.RsaLength;
import org.pgpainless.key.info.KeyRingInfo;
import org.pgpainless.key.protection.SecretKeyRingProtector;

/**
 * PGPainless 1.7.6 driven the way the Android app (crypto/Pgp.kt) drives it.
 *
 *   gen <ed25519|rsa3072> <name> <outdir>    writes secret.asc + public.asc, prints FPR
 *   fpr <public.asc>                         prints FPR and what PGPainless makes of the key
 *   encrypt <plain> <out.asc> <signer-secret.asc> <recipient-public.asc>...
 *                                            Pgp.encrypt(): sign + encrypt, armored, default compression
 *   decrypt <in.asc> <secret.asc> [signer-public.asc]
 *                                            Pgp.decryptOrNull() / Pgp.decryptSignedBy()
 *
 * Extra paths the app also has, for completeness:
 *   encrypt-nosign <plain> <out.asc> <recipient-public.asc>...       Pgp.encrypt(sign = false)
 *   encrypt-stream <plain> <out.pgp> <signer-secret.asc> <recipient>... Pgp.encryptStream(): binary, uncompressed
 *   sign <plain> <out.asc> <signer-secret.asc>                       Pgp.sign(): signed, not encrypted
 * and one the app does NOT do, to isolate compression in a failure:
 *   encrypt-uncompressed <plain> <out.asc> <signer-secret.asc> <recipient>...
 *   bench <ed25519|rsa3072> <rounds> <peer-public.asc>               warm timings inside one JVM
 *
 * Output is line based ("KEY value"), UTF-8, so a test driver can parse it.
 * Any failure prints "ERROR <exception class>: <message>" plus its causes and exits 2.
 */
public class Interop {

    private static final PrintStream OUT =
            new PrintStream(new FileOutputStream(FileDescriptor.out), true, StandardCharsets.UTF_8);

    public static void main(String[] args) {
        quietSlf4j();
        if (args.length == 0) {
            usage();
        }
        try {
            switch (args[0]) {
                case "gen" -> gen(args);
                case "fpr" -> fpr(args);
                case "encrypt" -> produce(args, true, true, true, false);
                case "encrypt-nosign" -> produce(args, true, false, true, false);
                case "encrypt-stream" -> produce(args, true, true, false, true);
                case "encrypt-uncompressed" -> produce(args, true, true, true, true);
                case "sign" -> produce(args, false, true, true, false);
                case "decrypt" -> decrypt(args);
                case "bench" -> bench(args);
                default -> usage();
            }
        } catch (Throwable t) {
            OUT.println("ERROR " + t.getClass().getName() + ": " + t.getMessage());
            for (Throwable c = t.getCause(); c != null; c = c.getCause()) {
                OUT.println("CAUSE " + c.getClass().getName() + ": " + c.getMessage());
            }
            OUT.flush();
            System.exit(2);
        }
    }

    /**
     * slf4j-api 1.7 with no binding on the classpath prints a three line
     * "Failed to load class org.slf4j.impl.StaticLoggerBinder" warning on the
     * first logger lookup and then falls back to the NOP logger. Let that first
     * lookup happen while stderr points nowhere. INTEROP_SHOW_SLF4J=1 keeps it.
     */
    private static void quietSlf4j() {
        if ("1".equals(System.getenv("INTEROP_SHOW_SLF4J"))) {
            return;
        }
        PrintStream err = System.err;
        try {
            System.setErr(new PrintStream(OutputStream.nullOutputStream()));
            org.slf4j.LoggerFactory.getILoggerFactory();
        } finally {
            System.setErr(err);
        }
    }

    private static void usage() {
        OUT.println("usage: gen <ed25519|rsa3072> <name> <outdir> | fpr <public.asc> | "
                + "encrypt <plain> <out.asc> <signer-secret.asc> <recipient.asc>... | "
                + "decrypt <in.asc> <secret.asc> [signer-public.asc] | "
                + "encrypt-nosign | encrypt-stream | encrypt-uncompressed | sign | bench");
        System.exit(64);
    }

    private static PGPSecretKeyRing generate(String type, String identity) throws Exception {
        return switch (type) {
            case "ed25519" -> PGPainless.generateKeyRing().modernKeyRing(identity);
            case "rsa3072" -> PGPainless.generateKeyRing().rsaKeyRing(identity, RsaLength._3072);
            default -> throw new IllegalArgumentException("key type must be ed25519 or rsa3072, got " + type);
        };
    }

    private static void gen(String[] a) throws Exception {
        Path dir = Path.of(a[3]);
        Files.createDirectories(dir);

        long t0 = System.nanoTime();
        PGPSecretKeyRing secretKeys = generate(a[1], a[2]);
        long t1 = System.nanoTime();

        String armoredSecret = PGPainless.asciiArmor(secretKeys);
        PGPPublicKeyRing certificate = PGPainless.extractCertificate(secretKeys);
        String fingerprint = OpenPgpFingerprint.of(secretKeys).toString();

        Files.writeString(dir.resolve("secret.asc"), armoredSecret, StandardCharsets.UTF_8);
        Files.writeString(dir.resolve("public.asc"), PGPainless.asciiArmor(certificate), StandardCharsets.UTF_8);

        OUT.println("FPR " + fingerprint);
        OUT.println("GEN_MS " + ms(t0, t1));
        describe(certificate);
    }

    private static void fpr(String[] a) throws Exception {
        PGPPublicKeyRing cert = readPublic(a[1]);
        OUT.println("FPR " + OpenPgpFingerprint.of(cert));
        describe(cert);
    }

    /** The app's producers. Argument layout: <plain> <out> [<signer-secret>] <recipient>... */
    private static void produce(String[] a, boolean encrypt, boolean sign, boolean armor, boolean uncompressed)
            throws Exception {
        byte[] plaintext = Files.readAllBytes(Path.of(a[1]));
        Path target = Path.of(a[2]);
        int next = 3;
        PGPSecretKeyRing secretKeys = null;
        if (sign) {
            secretKeys = readSecret(a[next++]);
        }

        EncryptionOptions encryption = new EncryptionOptions();
        if (encrypt) {
            for (int i = next; i < a.length; i++) {
                encryption.addRecipient(readPublic(a[i]));
            }
        }
        SigningOptions signing = null;
        if (sign) {
            signing = new SigningOptions().addInlineSignature(
                    SecretKeyRingProtector.unprotectedKeys(), secretKeys, DocumentSignatureType.BINARY_DOCUMENT);
        }
        ProducerOptions options;
        if (encrypt && sign) {
            options = ProducerOptions.signAndEncrypt(encryption, signing);
        } else if (encrypt) {
            options = ProducerOptions.encrypt(encryption);
        } else {
            options = ProducerOptions.sign(signing);
        }
        options.setAsciiArmor(armor);
        if (uncompressed) {
            options.overrideCompressionAlgorithm(CompressionAlgorithm.UNCOMPRESSED);
        }

        ByteArrayOutputStream out = new ByteArrayOutputStream();
        long t0 = System.nanoTime();
        EncryptionStream stream = PGPainless.encryptAndOrSign()
                .onOutputStream(out)
                .withOptions(options);
        Streams.pipeAll(new ByteArrayInputStream(plaintext), stream);
        stream.close();
        long t1 = System.nanoTime();

        Files.write(target, out.toByteArray());
        EncryptionResult result = stream.getResult();
        OUT.println("SYMMETRIC " + result.getEncryptionAlgorithm());
        OUT.println("COMPRESSION " + result.getCompressionAlgorithm());
        for (SubkeyIdentifier recipient : result.getRecipients()) {
            OUT.println("RECIPIENT " + recipient);
        }
        OUT.println("ENCRYPT_MS " + ms(t0, t1));
        OUT.println("OK");
    }

    private static void decrypt(String[] a) throws Exception {
        byte[] message = Files.readAllBytes(Path.of(a[1]));
        PGPSecretKeyRing secretKeys = readSecret(a[2]);
        PGPPublicKeyRing signerCert = a.length > 3 ? readPublic(a[3]) : null;

        long t0 = System.nanoTime();
        Decrypted d = decryptOnce(message, secretKeys, signerCert);
        long t1 = System.nanoTime();

        MessageMetadata metadata = d.metadata;
        OUT.println("PLAINTEXT_B64 " + Base64.getEncoder().encodeToString(d.plaintext));
        OUT.println("PLAINTEXT_BEGIN");
        OUT.println(new String(d.plaintext, StandardCharsets.UTF_8));
        OUT.println("PLAINTEXT_END");
        OUT.println("SIG_VERIFIED " + (signerCert != null && metadata.isVerifiedSignedBy(signerCert)));
        for (SignatureVerification v : metadata.getVerifiedSignatures()) {
            OUT.println("VERIFIED_SIG v" + v.getSignature().getVersion() + " by " + v.getSigningKey());
        }
        for (SignatureVerification.Failure f : metadata.getRejectedSignatures()) {
            OUT.println("REJECTED_SIG by " + f.getSigningKey() + ": " + f.getValidationException().getMessage());
        }
        OUT.println("ENCRYPTED " + metadata.isEncrypted());
        OUT.println("SYMMETRIC " + metadata.getEncryptionAlgorithm());
        OUT.println("DECRYPTION_KEY " + metadata.getDecryptionKey());
        List<String> ids = new ArrayList<>();
        for (Long id : metadata.getRecipientKeyIds()) {
            ids.add(String.format("%016X", id));
        }
        OUT.println("RECIPIENT_KEY_IDS " + String.join(",", ids));
        OUT.println("COMPRESSION " + metadata.getCompressionAlgorithm());
        OUT.println("LITERAL_ENCODING " + metadata.getLiteralDataEncoding());
        OUT.println("DECRYPT_MS " + ms(t0, t1));
    }

    private record Decrypted(byte[] plaintext, MessageMetadata metadata) {
    }

    /** decryptOrNull() when signerCert is null, decryptSignedBy() otherwise. */
    private static Decrypted decryptOnce(byte[] message, PGPSecretKeyRing secretKeys, PGPPublicKeyRing signerCert)
            throws Exception {
        SecretKeyRingProtector protector = SecretKeyRingProtector.unprotectedKeys();
        ConsumerOptions options = new ConsumerOptions().addDecryptionKey(secretKeys, protector);
        if (signerCert != null) {
            options.addVerificationCert(signerCert)
                    .verifyNotAfter(new Date(System.currentTimeMillis() + 10 * 60 * 1000L));
        }
        DecryptionStream stream = PGPainless.decryptAndOrVerify()
                .onInputStream(new ByteArrayInputStream(message))
                .withOptions(options);
        ByteArrayOutputStream plain = new ByteArrayOutputStream();
        Streams.pipeAll(stream, plain);
        stream.close();
        return new Decrypted(plain.toByteArray(), stream.getMetadata());
    }

    /** Warm timings: same JVM, first rounds thrown away. */
    private static void bench(String[] a) throws Exception {
        String type = a[1];
        int rounds = Integer.parseInt(a[2]);
        PGPPublicKeyRing peer = readPublic(a[3]);
        byte[] plaintext = "Città 🌍 ✓\nriga due\tcon tab".getBytes(StandardCharsets.UTF_8);

        List<Double> gen = new ArrayList<>();
        PGPSecretKeyRing keys = null;
        for (int i = 0; i < rounds; i++) {
            long t0 = System.nanoTime();
            keys = generate(type, "Bench <bench@pgpchat.test>");
            gen.add((System.nanoTime() - t0) / 1e6);
        }
        PGPPublicKeyRing own = PGPainless.extractCertificate(keys);

        List<Double> enc = new ArrayList<>();
        byte[] ciphertext = null;
        for (int i = 0; i < rounds * 4; i++) {
            long t0 = System.nanoTime();
            EncryptionOptions encryption = new EncryptionOptions().addRecipient(peer).addRecipient(own);
            SigningOptions signing = new SigningOptions().addInlineSignature(
                    SecretKeyRingProtector.unprotectedKeys(), keys, DocumentSignatureType.BINARY_DOCUMENT);
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            EncryptionStream stream = PGPainless.encryptAndOrSign().onOutputStream(out)
                    .withOptions(ProducerOptions.signAndEncrypt(encryption, signing).setAsciiArmor(true));
            Streams.pipeAll(new ByteArrayInputStream(plaintext), stream);
            stream.close();
            ciphertext = out.toByteArray();
            enc.add((System.nanoTime() - t0) / 1e6);
        }

        List<Double> dec = new ArrayList<>();
        for (int i = 0; i < rounds * 4; i++) {
            long t0 = System.nanoTime();
            Decrypted d = decryptOnce(ciphertext, keys, own);
            if (!d.metadata.isVerifiedSignedBy(own)) {
                throw new IllegalStateException("bench: signature did not verify");
            }
            dec.add((System.nanoTime() - t0) / 1e6);
        }
        OUT.println("BENCH_GEN_MS " + summary(gen));
        OUT.println("BENCH_ENCRYPT_MS " + summary(enc));
        OUT.println("BENCH_DECRYPT_VERIFY_MS " + summary(dec));
    }

    private static String summary(List<Double> all) {
        List<Double> warm = new ArrayList<>(all.subList(Math.min(3, all.size() - 1), all.size()));
        Collections.sort(warm);
        double median = warm.get(warm.size() / 2);
        return String.format(Locale.ROOT, "first=%.1f median_warm=%.2f n=%d", all.get(0), median, all.size());
    }

    private static PGPSecretKeyRing readSecret(String path) throws Exception {
        String armored = Files.readString(Path.of(path), StandardCharsets.UTF_8);
        PGPSecretKeyRing keys = PGPainless.readKeyRing().secretKeyRing(armored);
        if (keys == null) {
            throw new IllegalArgumentException("no secret key ring in " + path);
        }
        return keys;
    }

    private static PGPPublicKeyRing readPublic(String path) throws Exception {
        String armored = Files.readString(Path.of(path), StandardCharsets.UTF_8);
        PGPPublicKeyRing keys = PGPainless.readKeyRing().publicKeyRing(armored);
        if (keys == null) {
            throw new IllegalArgumentException("no public key ring in " + path);
        }
        return keys;
    }

    /** What Bouncy Castle parsed and what PGPainless' policy makes of it. */
    private static void describe(PGPKeyRing keys) {
        Iterator<PGPPublicKey> it = keys.getPublicKeys();
        while (it.hasNext()) {
            PGPPublicKey k = it.next();
            OUT.println("KEY v" + k.getVersion()
                    + " algo=" + k.getAlgorithm()
                    + " bits=" + k.getBitStrength()
                    + " primary=" + k.isMasterKey()
                    + " encryptionCapable=" + k.isEncryptionKey()
                    + " fpr=" + Hex.toHexString(k.getFingerprint()).toUpperCase(Locale.ROOT));
        }
        try {
            KeyRingInfo info = PGPainless.inspectKeyRing(keys);
            OUT.println("INFO version=" + info.getVersion()
                    + " algorithm=" + info.getAlgorithm()
                    + " userIds=" + info.getUserIds()
                    + " usableForEncryption=" + info.isUsableForEncryption()
                    + " signingCapable=" + info.isSigningCapable());
            List<String> enc = new ArrayList<>();
            for (PGPPublicKey k : info.getEncryptionSubkeys(EncryptionPurpose.ANY)) {
                enc.add(Hex.toHexString(k.getFingerprint()).toUpperCase(Locale.ROOT));
            }
            OUT.println("INFO encryptionSubkeys=" + enc);
            OUT.println("INFO preferredSymmetric=" + info.getPreferredSymmetricKeyAlgorithms());
        } catch (Throwable t) {
            OUT.println("INFO_ERROR " + t.getClass().getName() + ": " + t.getMessage());
        }
    }

    private static String ms(long t0, long t1) {
        return String.format(Locale.ROOT, "%.1f", (t1 - t0) / 1_000_000.0);
    }
}
