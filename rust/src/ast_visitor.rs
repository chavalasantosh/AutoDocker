use std::collections::HashSet;

#[derive(Debug, Default, Clone)]
pub struct DependencyAstVisitor {
    pub required_imports: HashSet<String>,
    pub optional_imports: HashSet<String>,
    pub type_checking_imports: HashSet<String>,
    pub needs_stdin: bool,
    pub has_app_object: bool,
    pub app_object_name: Option<String>,
    pub detected_framework: Option<String>,
}

impl DependencyAstVisitor {
    pub fn new() -> Self {
        Self::default()
    }

    /// Parse and analyze Python source code line by line with lexical scope tracking.
    pub fn parse_source(&mut self, source: &str) {
        let mut in_main_guard = false;
        let mut main_guard_indent = 0;

        let mut in_type_checking = false;
        let mut type_checking_indent = 0;

        let mut in_try = false;
        let mut try_indent = 0;

        let mut in_conditional_if = false;
        let mut conditional_if_indent = 0;

        for line in source.lines() {
            let trimmed = line.trim();
            if trimmed.is_empty() || trimmed.starts_with('#') {
                continue;
            }

            let indent = line.len() - line.trim_start().len();

            // Check if we exited previously indented blocks
            if in_main_guard && indent <= main_guard_indent && !trimmed.starts_with("else:") && !trimmed.starts_with("elif ") {
                in_main_guard = false;
            }
            if in_type_checking && indent <= type_checking_indent && !trimmed.starts_with("else:") && !trimmed.starts_with("elif ") {
                in_type_checking = false;
            }
            if in_try && indent <= try_indent && (trimmed.starts_with("except") || trimmed.starts_with("finally:")) {
                // Keep in_try active during except/finally if still guarded
            } else if in_try && indent <= try_indent {
                in_try = false;
            }
            if in_conditional_if && indent <= conditional_if_indent && !trimmed.starts_with("else:") && !trimmed.starts_with("elif ") {
                in_conditional_if = false;
            }

            // Check for stdin usage
            if trimmed.contains("input(") || trimmed.contains("sys.stdin.read") || trimmed.contains("sys.stdin.readline") {
                self.needs_stdin = true;
            }

            // Check for framework instantiation
            if trimmed.contains("FastAPI(") {
                self.detected_framework = Some("fastapi".to_string());
                self.has_app_object = true;
                if let Some(var_name) = extract_variable_assignment(trimmed, "FastAPI(") {
                    self.app_object_name = Some(var_name);
                }
            } else if trimmed.contains("Flask(__name__)") || trimmed.contains("Flask(") {
                self.detected_framework = Some("flask".to_string());
                self.has_app_object = true;
                if let Some(var_name) = extract_variable_assignment(trimmed, "Flask(") {
                    self.app_object_name = Some(var_name);
                }
            } else if trimmed.contains("streamlit") && self.detected_framework.is_none() {
                self.detected_framework = Some("streamlit".to_string());
            } else if trimmed.contains("gradio") && self.detected_framework.is_none() {
                self.detected_framework = Some("gradio".to_string());
            }

            // Check block beginnings
            if trimmed.starts_with("if ") {
                let cond = trimmed.trim_start_matches("if ").trim_end_matches(':').trim();
                if cond.contains("__name__") && cond.contains("__main__") {
                    in_main_guard = true;
                    main_guard_indent = indent;
                    continue;
                } else if cond == "TYPE_CHECKING" || cond.contains("typing.TYPE_CHECKING") {
                    in_type_checking = true;
                    type_checking_indent = indent;
                    continue;
                } else if cond == "True" || cond == "__debug__" {
                    // Unconditional branch; treated as top-level required
                    continue;
                } else {
                    in_conditional_if = true;
                    conditional_if_indent = indent;
                    continue;
                }
            }

            if trimmed.starts_with("try:") {
                in_try = true;
                try_indent = indent;
                continue;
            }

            // Process imports
            if trimmed.starts_with("import ") {
                let raw = trimmed.trim_start_matches("import ").trim();
                for part in raw.split(',') {
                    let part = part.trim();
                    let module_full = part.split_whitespace().next().unwrap_or("").trim();
                    let top_pkg = module_full.split('.').next().unwrap_or("").trim();
                    if is_valid_identifier(top_pkg) {
                        self.record_import(top_pkg, in_type_checking, in_try, in_conditional_if, in_main_guard);
                    }
                }
            } else if trimmed.starts_with("from ") {
                let raw = trimmed.trim_start_matches("from ").trim();
                if let Some(idx) = raw.find(" import ") {
                    let mod_path = raw[..idx].trim();
                    let top_pkg = mod_path.split('.').next().unwrap_or("").trim();
                    if is_valid_identifier(top_pkg) {
                        self.record_import(top_pkg, in_type_checking, in_try, in_conditional_if, in_main_guard);
                    }
                }
            }
        }
    }

    fn record_import(
        &mut self,
        top_pkg: &str,
        in_type_checking: bool,
        in_try: bool,
        in_conditional_if: bool,
        in_main_guard: bool,
    ) {
        if in_type_checking {
            self.type_checking_imports.insert(top_pkg.to_string());
            return;
        }

        if in_try {
            if !self.required_imports.contains(top_pkg) {
                self.optional_imports.insert(top_pkg.to_string());
            }
        } else if in_conditional_if && !in_main_guard {
            if !self.required_imports.contains(top_pkg) {
                self.optional_imports.insert(top_pkg.to_string());
            }
        } else {
            self.optional_imports.remove(top_pkg);
            self.required_imports.insert(top_pkg.to_string());
        }
    }

    pub fn all_runtime_imports(&self) -> HashSet<String> {
        let mut all = self.required_imports.clone();
        for opt in &self.optional_imports {
            all.insert(opt.clone());
        }
        all
    }
}

fn extract_variable_assignment(line: &str, keyword: &str) -> Option<String> {
    if let Some(pos) = line.find(keyword) {
        let prefix = line[..pos].trim();
        if let Some(eq_pos) = prefix.find('=') {
            let var = prefix[..eq_pos].trim();
            // In case of type annotation `app: FastAPI = FastAPI()`
            let clean_var = var.split(':').next().unwrap_or("").trim();
            if is_valid_identifier(clean_var) {
                return Some(clean_var.to_string());
            }
        }
    }
    None
}

fn is_valid_identifier(s: &str) -> bool {
    if s.is_empty() {
        return false;
    }
    let mut chars = s.chars();
    let first = chars.next().unwrap();
    if !first.is_alphabetic() && first != '_' {
        return false;
    }
    chars.all(|c| c.is_alphanumeric() || c == '_')
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_ast_visitor_categorization() {
        let code = r#"
import os
import sys
import numpy
from pandas import DataFrame

try:
    import catboost
except ImportError:
    pass

if feature_enabled:
    import lightgbm

if True:
    import scipy

if __name__ == '__main__':
    import uvicorn
"#;
        let mut visitor = DependencyAstVisitor::new();
        visitor.parse_source(code);

        assert!(visitor.required_imports.contains("os"));
        assert!(visitor.required_imports.contains("numpy"));
        assert!(visitor.required_imports.contains("pandas"));
        assert!(visitor.required_imports.contains("scipy"));
        assert!(visitor.required_imports.contains("uvicorn"));

        assert!(visitor.optional_imports.contains("catboost"));
        assert!(visitor.optional_imports.contains("lightgbm"));
        assert!(!visitor.required_imports.contains("catboost"));
        assert!(!visitor.required_imports.contains("lightgbm"));
    }

    #[test]
    fn test_type_checking_imports_excluded() {
        let code = r#"
from typing import TYPE_CHECKING
import os

if TYPE_CHECKING:
    import mypy_boto3_s3
    import pydantic

if __name__ == '__main__':
    import uvicorn
"#;
        let mut visitor = DependencyAstVisitor::new();
        visitor.parse_source(code);

        assert!(visitor.required_imports.contains("os"));
        assert!(visitor.required_imports.contains("uvicorn"));
        assert!(!visitor.required_imports.contains("mypy_boto3_s3"));
        assert!(!visitor.required_imports.contains("pydantic"));
        assert!(visitor.type_checking_imports.contains("mypy_boto3_s3"));
        assert!(visitor.type_checking_imports.contains("pydantic"));
    }
}

