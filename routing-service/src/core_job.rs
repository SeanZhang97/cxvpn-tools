//! Ensure an abrupt service exit cannot leave its privileged core sending on orphan routes.
use crate::util::AppResult;
use std::{os::windows::io::AsRawHandle, process::Child};
use windows_sys::Win32::{Foundation::{CloseHandle, HANDLE},
    System::JobObjects::{AssignProcessToJobObject, CreateJobObjectW, SetInformationJobObject,
        JobObjectExtendedLimitInformation, JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE}};

pub struct CoreJob(usize);
impl CoreJob {
    pub fn attach(child: &Child) -> AppResult<Self> {
        let handle = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
        if handle.is_null() { return Err("create core process job failed".into()); }
        let job = Self(handle as usize);
        let mut info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if unsafe { SetInformationJobObject(handle, JobObjectExtendedLimitInformation,
            (&info as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(), std::mem::size_of_val(&info) as u32) } == 0
            || unsafe { AssignProcessToJobObject(handle, child.as_raw_handle() as HANDLE) } == 0 {
            return Err("attach core process job failed".into());
        }
        Ok(job)
    }
}
impl Drop for CoreJob { fn drop(&mut self) { unsafe { CloseHandle(self.0 as HANDLE); } } }
