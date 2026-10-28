//! Opt-in in-process Rust view adapter for the local Python Control Room.
//! Never stores graph data across calls, opens a socket, reads credentials,
//! executes source code or authorizes infrastructure actions.
use ontologyex_core::view;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use serde_json::Value;

const MAX_GRAPH_BYTES: usize = 16 * 1024 * 1024;

fn decode(raw: &str) -> PyResult<Value> {
    if raw.len() > MAX_GRAPH_BYTES {
        return Err(PyValueError::new_err("graph input exceeds native byte budget"));
    }
    serde_json::from_str(raw)
        .map_err(|_| PyValueError::new_err("invalid source graph JSON"))
}
fn encode(result: Result<Value, String>) -> PyResult<String> {
    let value = result.map_err(PyValueError::new_err)?;
    serde_json::to_string(&value)
        .map_err(|_| PyValueError::new_err("cannot serialize native graph view"))
}

#[pyfunction]
fn summary_json(graph_json: &str) -> PyResult<String> {
    encode(view::summary(&decode(graph_json)?))
}
#[pyfunction]
fn page_json(graph_json: &str, lens: &str, query: &str, offset: usize) -> PyResult<String> {
    encode(view::page(&decode(graph_json)?,lens,query,offset))
}
#[pyfunction]
fn neighborhood_json(graph_json: &str, center_id: &str, depth: usize) -> PyResult<String> {
    encode(view::neighborhood(&decode(graph_json)?,center_id,depth))
}
#[pyfunction]
fn node_json(graph_json: &str, node_id: &str) -> PyResult<String> {
    encode(view::node_detail(&decode(graph_json)?,node_id))
}

#[pymodule]
fn ontologyex_native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(summary_json, module)?)?;
    module.add_function(wrap_pyfunction!(page_json, module)?)?;
    module.add_function(wrap_pyfunction!(neighborhood_json, module)?)?;
    module.add_function(wrap_pyfunction!(node_json, module)?)?;
    Ok(())
}
