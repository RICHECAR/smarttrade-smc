"""Read and extract key results from chantier12 output."""
import sys
try:
    with open("chantier12_output.txt", "r", encoding="utf-8") as f:
        content = f.read()
    
    # Write filtered output to new file
    out_lines = []
    for line in content.split("\n"):
        s = line.strip()
        if "HTTP" in s:
            continue
        # Skip unicode box-drawing characters
        if any(c in s for c in ["═","─","║","╔","╗","╚","╝","█","▓","░"]):
            if len(s) > 60 and s.count("═") > 10:
                continue
        out_lines.append(s)
    
    result = "\n".join(out_lines)
    with open("c12_results.txt", "w", encoding="utf-8") as f:
        f.write(result)
    
    print(f"OK: {len(result)} chars written to c12_results.txt")
    
    # Also print key facts
    for kw in ["RUNNING COMBINED", "COMPARISON TABLE", "VERDICT FINAL", "NO EDGE", "EDGE FOUND", "Report saved"]:
        idx = content.find(kw)
        print(f"  {kw}: {'FOUND at '+str(idx) if idx >= 0 else 'NOT FOUND'}")
    
except Exception as e:
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)
