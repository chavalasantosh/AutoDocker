use std::net::{SocketAddr, TcpListener};
use crate::models::{Inspection, PublishedPort};

pub struct PortAudit;

impl PortAudit {
    pub fn check(port: &PublishedPort) -> (bool, String) {
        let addr = format!("127.0.0.1:{}", port.host);
        match addr.parse::<SocketAddr>() {
            Ok(sa) => match TcpListener::bind(sa) {
                Ok(_) => (true, "available now (not reserved; another process may claim it)".to_string()),
                Err(e) => (false, format!("BUSY/UNAVAILABLE: {}", e)),
            },
            Err(e) => (false, format!("Invalid address: {}", e)),
        }
    }

    pub fn print_report(ports: &[PublishedPort]) -> bool {
        println!("\nHost port preflight (127.0.0.1; advisory, no ports reserved)");
        let mut all_free = true;
        for p in ports {
            let (free, msg) = Self::check(p);
            let badge = if free { "OK  " } else { "BUSY" };
            println!("  {} {:8} {:5} -> {:5}: {}", badge, p.service, p.host, p.container, msg);
            if !free {
                all_free = false;
            }
        }
        all_free
    }
}

pub struct DependencyAudit;

impl DependencyAudit {
    pub fn print_report(inspection: &Inspection) {
        println!("\nAuto Docker dependency preflight (static, read-only)");
        if let Some(ref mf) = inspection.dependency {
            println!("  Declared manifest: {}", mf);
        } else {
            println!("  Explicitly declared packages: (none found)");
        }

        let mut req: Vec<&String> = inspection.entry.required_imports.iter().collect();
        req.sort();
        if !req.is_empty() {
            println!("  Required candidate packages: {}", req.into_iter().cloned().collect::<Vec<_>>().join(", "));
        }

        let mut opt: Vec<&String> = inspection.entry.optional_imports.iter().collect();
        opt.sort();
        if !opt.is_empty() {
            println!("  Optional candidate packages (guarded): {}", opt.into_iter().cloned().collect::<Vec<_>>().join(", "));
        }

        for w in &inspection.warnings {
            println!("  WARNING: {}", w);
        }
    }
}

