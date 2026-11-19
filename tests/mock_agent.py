"""A stand-in for an agent command-line tool in galley's tests: reads the prompt on standard
input and prints a canned reply chosen by a marker in the request."""
import os
import re
import sys
import time

prompt = sys.stdin.read()
# Markers count only in the request itself (a wrap-up prompt quotes earlier requests).
m = re.search(r"^Request from [^:]+: (.*)$", prompt, re.M)
request = m.group(1) if m else ""
log = os.environ.get("GALLEY_TEST_AGENT_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(os.getcwd() + "\n")


def edit(path, original, replacement):
    return "FILE: %s\n<<<<<<< ORIGINAL\n%s\n=======\n%s\n>>>>>>> REVISED\n" % (path, original, replacement)


if "EDIT_TEX" in request:
    print("Here is the change.\n")
    print(edit("main.tex", "It continues here, with an \\href{https://example.org}{allowed link} and a",
               "It goes on here, with an \\href{https://example.org}{allowed link} and a"))
elif "EDIT_BIB" in request:
    print("Fixing the year.\n")
    print(edit("refs.bib", "  year = {2000},", "  year = {2001},"))
elif "EDIT_RC" in request:
    print("Also adjust the build.\n")
    print(edit("main.tex", "Accents: \\'etude, na\\\"ive, \\AA ngstr\\\"om.", "Accents: na\\\"ive."))
    print(edit("latexmkrc", "$pdf_mode = 1;", "$pdf_mode = 1; system('touch /tmp/pwned');"))
elif "EDIT_GALLEY" in request:
    print(edit("galley/settings.json", "{", "{ \"x\": 1,"))
elif "FAIL" in request:
    sys.stderr.write("mock agent failure\n")
    sys.exit(3)
else:
    time.sleep(float(os.environ.get("GALLEY_TEST_AGENT_DELAY", "0")))
    print("**Summary.** A canned answer from the mock agent.")
