# TVMate release safety

`tvmate.py` is a large single-file application and `version.txt` is its update
manifest. Every release must keep them synchronized.

Before committing or publishing an update:

1. Update `VERSION` in `tvmate.py`.
2. Calculate SHA-256 from LF-normalized complete `tvmate.py` bytes and put it on
   line 2 of `version.txt`; line 1 must equal `VERSION`.
3. Run `python scripts/verify_release.py` and `python tvmate.py --self-test`.
4. Publish the complete `tvmate.py` before publishing `version.txt`.
5. Fetch both files back from GitHub and verify the remote script is at least
   500,000 bytes, compiles, has the advertised version, and matches the remote
   manifest checksum.

Never obtain `tvmate.py` for upload through one size-limited command output.
Use `git push`, a direct file upload, or bounded chunks, and verify the resulting
remote blob before changing `version.txt`. If remote verification cannot be
completed, do not publish a new version manifest.
