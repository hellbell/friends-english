// Encrypt built Pages files in place with AES-256-GCM, using the Web Crypto API the browser decrypts with.
//   deno run --allow-read=DIR --allow-write=DIR seal.mjs DIR NAME... < 32-byte key
// Each file becomes IV (12 bytes) + ciphertext + tag. The relative path is the associated data,
// so one sealed file cannot be swapped in for another.
const [dir, ...names] = Deno.args;
const raw = new Uint8Array(await new Response(Deno.stdin.readable).arrayBuffer());
if (raw.length !== 32) throw new Error('Expected a 32-byte key on stdin');
const key = await crypto.subtle.importKey('raw', raw, 'AES-GCM', false, ['encrypt']);
for (const name of names) {
  const path = `${dir}/${name}`;
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const sealed = await crypto.subtle.encrypt({ name: 'AES-GCM', iv, additionalData: new TextEncoder().encode(name) },
    key, await Deno.readFile(path));
  const out = new Uint8Array(12 + sealed.byteLength);
  out.set(iv);
  out.set(new Uint8Array(sealed), 12);
  await Deno.writeFile(path, out);
}
