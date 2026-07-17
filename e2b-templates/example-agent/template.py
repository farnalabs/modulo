from e2b import Template
import os

t = Template(file_context_path=os.path.dirname(os.path.abspath(__file__)))
b = t.from_template("base")

b.apt_install(["jq"])
b.pip_install(["requests", "pyyaml"])
b.npm_install(["@opencode-ai/cli"])
b.copy("modulo-wrap.sh", "/home/user/modulo-wrap.sh")
b.copy("lildax-review.sh", "/home/user/lildax-review.sh")
b.run_cmd("chmod +x /home/user/modulo-wrap.sh")
b.run_cmd("chmod +x /home/user/lildax-review.sh")

result = t.build(b, name="modulo-agent-lildax", tags=["modulo", "agent", "lildax", "default"])
print(f"Built: {result.template_id}")
