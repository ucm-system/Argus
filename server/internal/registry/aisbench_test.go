package registry

import (
	"os"
	"path/filepath"
	"testing"
)

func TestAISBenchRunnersRegisterAndValidateExamples(t *testing.T) {
	for _, name := range []string{"aisbench-acc", "aisbench-perf"} {
		t.Run(name, func(t *testing.T) {
			raw, err := os.ReadFile(filepath.Join("..", "..", "..", "runners", name, "runner.yaml"))
			if err != nil {
				t.Fatal(err)
			}
			spec, err := Parse(raw)
			if err != nil {
				t.Fatal(err)
			}
			if spec.Name != name || spec.Version != "0.3.5" {
				t.Fatalf("unexpected runner identity: %s %s", spec.Name, spec.Version)
			}
			if issues := ValidateInput(spec, spec.Placeholder); len(issues) != 0 {
				t.Fatalf("invalid placeholder: %+v", issues)
			}
			input := `{"datasets":["gsm8k_gen_4_shot_cot_str","mmlu_gen"],"num_prompts":10,"ignore_eos":false,"extra_info":"migration"}`
			if issues := ValidateInput(spec, input); len(issues) != 0 {
				t.Fatalf("invalid native dataset input: %+v", issues)
			}
			if name == "aisbench-perf" {
				if issues := ValidateInput(spec, `{"test_cases":[[256,32,16,50,2,1]]}`); len(issues) != 0 {
					t.Fatalf("invalid Jenkins prefix input: %+v", issues)
				}
				input := `{"metrics_endpoint":["http://127.0.0.1:7800/metrics"],"rounds":[{"input_len":256,"seed":1},{"input_len":512,"seed":2}],"repeat_rate":"50%"}`
				if issues := ValidateInput(spec, input); len(issues) != 0 {
					t.Fatalf("invalid multi-round input: %+v", issues)
				}
			}
		})
	}
}
