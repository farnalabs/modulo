import re, sys
path = sys.argv[1]
with open(path) as f:
    c = f.read()
c = c.replace(chr(92)+chr(34), chr(34))
with open(path, chr(119)) as f:
    f.write(c)
print(chr(68)+chr(111)+chr(110)+chr(101))
