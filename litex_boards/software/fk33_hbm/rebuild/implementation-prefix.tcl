set_param synth.maxThreads 1
set_param general.maxThreads 8
rename ::phys_opt_design ::fk33_native_phys_opt_design
proc ::phys_opt_design {args} {
    if {![info exists ::fk33_first_physopt_capture_done]} {
        write_checkpoint -force sqrl_fk33_pre_physopt.dcp
        report_timing -max_paths 100 -input_pins -file sqrl_fk33_pre_physopt_paths.rpt
        report_timing_summary -file sqrl_fk33_pre_physopt_summary.rpt
        set ::fk33_first_physopt_capture_done 1
        puts FK33_PRE_PHYSOPT_CAPTURE_COMPLETE
    }
    return [uplevel 1 [linsert $args 0 ::fk33_native_phys_opt_design]]
}
