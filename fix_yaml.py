with open('.github/workflows/ci.yml', 'rb') as f:
    data = f.read()
cleaned = bytearray()
i = 0
while i < len(data):
    b = data[i]
    # Skip UTF-8 encoded C1 control chars (U+0080-U+009F = C2 80 - C2 9F)
    if b == 0xC2 and i + 1 < len(data) and 0x80 <= data[i+1] <= 0x9F:
        i += 2
        continue
    cleaned.append(b)
    i += 1
text = cleaned.decode('utf-8')
text = text.replace('actions/checkout@v7', 'actions/checkout@v4')
with open('.github/workflows/ci.yml', 'w', encoding='utf-8') as f:
    f.write(text)
import yaml
yaml.safe_load(text)
print("VALID: YAML parses successfully")
print(f"Size: {len(data)} -> {len(text.encode('utf-8'))}")
