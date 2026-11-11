use std::collections::HashSet;
use rustpython_parser::ast::{self, CmpOp, Expr, Mod, Stmt};
use rustpython_parser::Mode;

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

#[derive(Default, Clone, Copy)]
struct AstWalkContext {
    in_try: bool,
    in_type_checking: bool,
    in_main_guard: bool,
    in_conditional: bool,
}

impl DependencyAstVisitor {
    pub fn new() -> Self {
        Self::default()
    }

    /// Parse and analyze Python source code using genuine Python AST parsing.
    /// Falls back to lexical analysis only if syntax is unparsable.
    pub fn parse_source(&mut self, source: &str) {
        if let Ok(parsed) = rustpython_parser::parse(source, Mode::Module, "<source>") {
            match parsed {
                Mod::Module(m) => {
                    let ctx = AstWalkContext::default();
                    for stmt in &m.body {
                        self.walk_stmt(stmt, ctx);
                    }
                    return;
                }
                _ => {}
            }
        }
        // Fallback for non-standard / invalid Python code
        self.parse_source_fallback(source);
    }

    fn walk_stmt(&mut self, stmt: &Stmt, ctx: AstWalkContext) {
        match stmt {
            Stmt::Import(import_stmt) => {
                for alias in &import_stmt.names {
                    let top_pkg = alias.name.as_str().split('.').next().unwrap_or("").trim();
                    if is_valid_identifier(top_pkg) {
                        self.record_import(top_pkg, ctx);
                    }
                }
            }
            Stmt::ImportFrom(from_stmt) => {
                if let Some(ref mod_name) = from_stmt.module {
                    let top_pkg = mod_name.as_str().split('.').next().unwrap_or("").trim();
                    if is_valid_identifier(top_pkg) {
                        self.record_import(top_pkg, ctx);
                    }
                }
            }
            Stmt::If(if_stmt) => {
                let is_main = is_main_guard_expr(&if_stmt.test);
                let is_tc = is_type_checking_expr(&if_stmt.test);
                let is_uncond = is_unconditional_expr(&if_stmt.test);

                let mut body_ctx = ctx;
                if is_main {
                    body_ctx.in_main_guard = true;
                } else if is_tc {
                    body_ctx.in_type_checking = true;
                } else if !is_uncond {
                    body_ctx.in_conditional = true;
                }

                for s in &if_stmt.body {
                    self.walk_stmt(s, body_ctx);
                }

                let mut else_ctx = ctx;
                if !is_uncond && !is_main && !is_tc {
                    else_ctx.in_conditional = true;
                }
                for s in &if_stmt.orelse {
                    self.walk_stmt(s, else_ctx);
                }
            }
            Stmt::Try(try_stmt) => {
                let mut try_ctx = ctx;
                try_ctx.in_try = true;
                for s in &try_stmt.body {
                    self.walk_stmt(s, try_ctx);
                }
                for handler in &try_stmt.handlers {
                    match handler {
                        ast::ExceptHandler::ExceptHandler(h) => {
                            for s in &h.body {
                                self.walk_stmt(s, try_ctx);
                            }
                        }
                    }
                }
                for s in &try_stmt.orelse {
                    self.walk_stmt(s, try_ctx);
                }
                for s in &try_stmt.finalbody {
                    self.walk_stmt(s, try_ctx);
                }
            }
            Stmt::FunctionDef(fn_stmt) => {
                let mut fn_ctx = ctx;
                fn_ctx.in_conditional = true;
                for s in &fn_stmt.body {
                    self.walk_stmt(s, fn_ctx);
                }
            }
            Stmt::AsyncFunctionDef(fn_stmt) => {
                let mut fn_ctx = ctx;
                fn_ctx.in_conditional = true;
                for s in &fn_stmt.body {
                    self.walk_stmt(s, fn_ctx);
                }
            }
            Stmt::ClassDef(class_stmt) => {
                for s in &class_stmt.body {
                    self.walk_stmt(s, ctx);
                }
            }
            Stmt::Assign(assign_stmt) => {
                self.check_framework_assignment(&assign_stmt.targets, &assign_stmt.value);
                self.check_expr_calls(&assign_stmt.value);
            }
            Stmt::AnnAssign(ann_assign) => {
                if let Some(ref val) = ann_assign.value {
                    let targets = vec![*ann_assign.target.clone()];
                    self.check_framework_assignment(&targets, val);
                    self.check_expr_calls(val);
                }
            }
            Stmt::Expr(expr_stmt) => {
                self.check_expr_calls(&expr_stmt.value);
            }
            _ => {}
        }
    }

    fn check_framework_assignment(&mut self, targets: &[Expr], value: &Expr) {
        if let Expr::Call(call) = value {
            let func_name = match &*call.func {
                Expr::Name(n) => n.id.as_str(),
                Expr::Attribute(a) => a.attr.as_str(),
                _ => "",
            };

            if func_name == "FastAPI" {
                self.detected_framework = Some("fastapi".to_string());
                self.has_app_object = true;
                if let Some(target) = targets.first() {
                    if let Expr::Name(n) = target {
                        self.app_object_name = Some(n.id.to_string());
                    }
                }
            } else if func_name == "Flask" {
                self.detected_framework = Some("flask".to_string());
                self.has_app_object = true;
                if let Some(target) = targets.first() {
                    if let Expr::Name(n) = target {
                        self.app_object_name = Some(n.id.to_string());
                    }
                }
            }
        }
    }

    fn check_expr_calls(&mut self, expr: &Expr) {
        match expr {
            Expr::Call(call) => {
                match &*call.func {
                    Expr::Name(n) if n.id.as_str() == "input" => {
                        self.needs_stdin = true;
                    }
                    Expr::Attribute(a) => {
                        if a.attr.as_str() == "read" || a.attr.as_str() == "readline" {
                            if let Expr::Attribute(inner) = &*a.value {
                                if inner.attr.as_str() == "stdin" {
                                    self.needs_stdin = true;
                                }
                            }
                        }
                    }
                    _ => {}
                }
                for arg in &call.args {
                    self.check_expr_calls(arg);
                }
            }
            Expr::Compare(comp) => {
                self.check_expr_calls(&comp.left);
                for c in &comp.comparators {
                    self.check_expr_calls(c);
                }
            }
            _ => {}
        }
    }

    fn record_import(&mut self, top_pkg: &str, ctx: AstWalkContext) {
        if ctx.in_type_checking {
            self.type_checking_imports.insert(top_pkg.to_string());
            return;
        }

        if ctx.in_try || (ctx.in_conditional && !ctx.in_main_guard) {
            if !self.required_imports.contains(top_pkg) {
                self.optional_imports.insert(top_pkg.to_string());
            }
        } else {
            self.optional_imports.remove(top_pkg);
            self.required_imports.insert(top_pkg.to_string());
        }
    }

    fn parse_source_fallback(&mut self, source: &str) {
        for line in source.lines() {
            let trimmed = line.trim();
            if trimmed.starts_with("import ") {
                let raw = trimmed.trim_start_matches("import ").trim();
                for part in raw.split(',') {
                    let top = part.trim().split_whitespace().next().unwrap_or("").split('.').next().unwrap_or("");
                    if is_valid_identifier(top) {
                        self.required_imports.insert(top.to_string());
                    }
                }
            } else if trimmed.starts_with("from ") {
                let raw = trimmed.trim_start_matches("from ").trim();
                if let Some(idx) = raw.find(" import ") {
                    let top = raw[..idx].trim().split('.').next().unwrap_or("");
                    if is_valid_identifier(top) {
                        self.required_imports.insert(top.to_string());
                    }
                }
            }
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

fn is_main_guard_expr(expr: &Expr) -> bool {
    match expr {
        Expr::Compare(comp) => {
            let left_is_name = match &*comp.left {
                Expr::Name(n) => n.id.as_str() == "__name__",
                _ => false,
            };
            let has_eq = comp.ops.iter().any(|op| matches!(op, CmpOp::Eq));
            let right_is_main = comp.comparators.iter().any(|c| match c {
                Expr::Constant(const_val) => match &const_val.value {
                    ast::Constant::Str(s) => s.as_str() == "__main__",
                    _ => false,
                },
                _ => false,
            });
            left_is_name && has_eq && right_is_main
        }
        _ => false,
    }
}

fn is_type_checking_expr(expr: &Expr) -> bool {
    match expr {
        Expr::Name(n) => n.id.as_str() == "TYPE_CHECKING",
        Expr::Attribute(a) => a.attr.as_str() == "TYPE_CHECKING",
        _ => false,
    }
}

fn is_unconditional_expr(expr: &Expr) -> bool {
    match expr {
        Expr::Constant(c) => match &c.value {
            ast::Constant::Bool(b) => *b,
            _ => false,
        },
        Expr::Name(n) => n.id.as_str() == "__debug__",
        _ => false,
    }
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

    #[test]
    fn test_framework_app_detection() {
        let code = r#"
from fastapi import FastAPI
app = FastAPI()

def main():
    x = input("Enter name: ")
"#;
        let mut visitor = DependencyAstVisitor::new();
        visitor.parse_source(code);

        assert_eq!(visitor.detected_framework.as_deref(), Some("fastapi"));
        assert!(visitor.has_app_object);
        assert_eq!(visitor.app_object_name.as_deref(), Some("app"));
        assert!(visitor.needs_stdin);
    }
}
