#!/usr/bin/env python
"""
This runs the freesurfer pipeline on T1 images.
Also now extracts some volumes and converts some files to nifty for epitome

Usage:
  dm-proc-freesurfer.py [options] <inputdir> <FS_subjectsdir>

Arguments:
    <inputdir>                Top directory for nii inputs normally (project/data/nii/)
    <FS_subjectsdir>          Top directory for the Freesurfer output

Options:
  --no-postFS              Do not submit postprocessing script to the queue
  --postFS-only            Only run the post freesurfer analysis
  --T1-tag STR             Tag used to find the T1 files (default is 'T1')
  --tag2 STR               Optional tag used (as well as '--T1-tag') to filter for correct input
  --multiple-inputs        Allow multiple input T1 files to Freesurfersh
  --FS-option STR          A quoted string of an non-default freesurfer option to add.
  --run-version STR        A version string that is appended to 'run_freesurfer_<tag>.sh' for multiple versions
  --QC-transfer QCFILE     QC checklist file - if this option is given than only QCed participants will be processed.
  --prefix STR             A prefix string (used by the ENIGMA Extract) to filter to subject ids.
  --walltime TIME          A walltime for the FS stage [default: 24:00:00]
  --walltime-post TIME     A walltime for the final stage [default: 2:00:00]
  -v,--verbose             Verbose logging
  --debug                  Debug logging in Erin's very verbose style
  -n,--dry-run             Dry run
  -h, --help               Show help

DETAILS
This runs the freesurfer pipeline on T1 images maps after conversion to nifty.

This script will look search inside the "inputdir" folder for T1 images to process.
It uses the '--T1-tag' string (which is '_T1_' by default) to identify them.
If the optional argument '--tag2' is given, this string will be used to refine
the search if more than one T1 file is found inside the participant's directory.

The T1 image found for each participant in printed in the 'T1_nii' column
of "freesurfer-checklist.csv". If no T1 image is found, or more than one T1 image
is found, a note to that effect is printed in the "notes" column of the same file.
You can manually overide this process by editing the "freesurfer-checklist.csv"
with the name of the T1 image you would like processed (esp. in the case of repeat scans).

The script then looks to see if any of the T1 images (listed in the
"freesurfer-checklist.csv" "T1_nii" column) have not been processed (i.e. have no outputs).
These images are then submitted to the queue.

If the "--QC-transfer" option is used, the QC checklist from data transfer
(i.e. metadata/checklist.csv) and only those participants who passed QC will be processed.

The '--run-version' option was added for situations when you might want to use
different freesurfer settings for a subgroup of your participants (for example,
all subjects from a site with an older scanner (but have all the
outputs show up in the same folder in the end). The run version string is appended
to the freesurfer_run.sh script name. Which allows for mutliple freesurfer_run.sh
scripts to exists in the bin folder.

Requires freesurfer and datman in the environment

The nifty conversion (sink) processing steps requires
AFNI and datman in the environment

Written by Erin W Dickie, Sep 30 2015
Adapted from old dm-proc-freesurfer.py
"""
import os
import sys
import glob
import time
import datetime
import tempfile
import shutil
import filecmp
import difflib
import contextlib
import logging

from docopt import docopt
import pandas as pd

import datman as dm

logger = logging.getLogger(__name__)
logger.setLevel(logging.WARN)

DRYRUN = False

def main():
    arguments       = docopt(__doc__)
    input_dir       = arguments['<inputdir>']
    output_dir      = arguments['<FS_subjectsdir>']
    QC_file         = arguments['--QC-transfer']
    MULTI_T1        = arguments['--multiple-inputs']
    T1_tag          = arguments['--T1-tag']
    TAG2            = arguments['--tag2']
    RUN_TAG         = arguments['--run-version']
    FS_option       = arguments['--FS-option']
    prefix          = arguments['--prefix']
    NO_POST         = arguments['--no-postFS']
    POSTFS_ONLY     = arguments['--postFS-only']
    walltime        = arguments['--walltime']
    walltime_post   = arguments['--walltime-post']
    verbose         = arguments['--verbose']
    debug           = arguments['--debug']
    DRYRUN          = arguments['--dry-run']

    if verbose:
        logger.setLevel(logging.INFO)

    if debug:
        logger.setLevel(logging.DEBUG)

    ## make the output directory if it doesn't exist
    output_dir = os.path.abspath(output_dir)
    log_dir = os.path.join(output_dir,'logs')
    run_dir = os.path.join(output_dir,'bin')
    dm.utils.makedirs(log_dir)
    dm.utils.makedirs(run_dir)

    logger.debug("Arguments: {}".format(arguments))

    if T1_tag is None: T1_tag = '_T1_'

    if post_settings_conflict(NO_POST, POSTFS_ONLY):
        logger.error("--no-postFS and --postFS-only cannot both be set. Exiting")
        sys.exit(1)

    subjects = dm.proc.get_subject_list(input_dir, TAG2, QC_file)

    # check if we have any work to do, exit if not
    if len(subjects) == 0:
        logger.info('No outstanding scans to process.')
        sys.exit(1)

    # grab the prefix from the subid if not given
    if prefix is None:
        prefix = subjects[0][0:3]

    script_names = get_run_script_names(RUN_TAG, POSTFS_ONLY, NO_POST)
    write_run_scripts(script_names, run_dir, output_dir, FS_option, prefix)

    checklist_file = os.path.normpath(output_dir + '/freesurfer-checklist.csv')
    columns = ['id', 'T1_nii', 'date_ran','qc_rator', 'qc_rating', 'notes']
    checklist = dm.proc.load_checklist(checklist_file, columns)
    checklist = dm.proc.add_new_subjects_to_checklist(subjects,
            checklist, columns)
    # Update checklist for subjects with no T1 listed under T1_nii
    checklist = dm.proc.find_images(checklist, 'T1_nii', input_dir, T1_tag,
            subject_filter=TAG2, allow_multiple=MULTI_T1)

    job_name_prefix="FS_{}_".format(datetime.datetime.today().strftime("%Y%m%d-%H%M%S"))
    submitted = False

    ## Change dir so it can be submitted without the full path
    os.chdir(run_dir)
    if not POSTFS_ONLY:
        for i in range(0,len(checklist)):
            subid = checklist['id'][i]

            # make sure that if TAG2 was called only the tag2s are going to queue
            if TAG2 and TAG2 not in subid:
                continue

            ## make sure that a T1 has been selected for this subject
            if pd.isnull(checklist['T1_nii'][i]):
                continue

            if subject_previously_completed(output_dir, subid):
                continue

            ## Check if subject was previously run and halted
            FSrunningglob = glob.glob(os.path.join(output_dir,subid,'scripts','IsRunning*'))
            FSrunning = FSrunningglob[0] if len(FSrunningglob) > 0 else ''

            if os.path.isfile(FSrunning):
                checklist['notes'][i] = "FS halted at {}".format(os.path.basename(FSrunning))
            else:
                ## format contents of T1 column into recon-all command input
                smap = checklist['T1_nii'][i]
                base_smaps = smap.split(';')
                T1s = []
                for basemap in base_smaps:
                    T1s.append('-i')
                    T1s.append(os.path.join(input_dir,subid,basemap))

                # If POSTFS_ONLY == False, the run script will be the first or
                # only name in the list
                script = script_names[0]
                FS_cmd = make_FS_command(run_dir, script, job_name_prefix,
                        log_dir, walltime, subid, T1s)
                docmd(FS_cmd)

                ## add today's date to the checklist
                checklist['date_ran'][i] = datetime.date.today()

                submitted = True

    ## if any subjects have been submitted,
    ## submit a final job that will consolidate the results after they are finished
    os.chdir(run_dir)
    if POSTFS_ONLY:
        script = script_names[0]
        post_FS_cmd = make_FS_command(run_dir, script, job_name_prefix,
                log_dir, walltime_post)
        docmd(post_FS_cmd)
    elif not NO_POST and submitted:
        script = script_names[1]
        post_FS_cmd = make_FS_command(run_dir, script, job_name_prefix,
                log_dir, walltime_post)
        docmd(post_FS_cmd)

    if not DRYRUN:
        ## write the checklist out to a file
        checklist.to_csv(checklist_file, sep=',', index = False)

def post_settings_conflict(NO_POST, POSTFS_ONLY):
    conflict = False
    if NO_POST and POSTFS_ONLY:
        conflict = True
    return conflict

def get_run_script_names(RUN_TAG, POSTFS_ONLY, NO_POST):
    """
    Return a list of script names for run scripts needed
    """
    ## set the basenames of the two run scripts
    if RUN_TAG == None:
        runFSsh_name = 'run_freesurfer.sh'
    else:
        runFSsh_name = 'run_freesurfer_' + RUN_TAG + '.sh'

    runPostsh_name = 'postfreesurfer.sh'

    if POSTFS_ONLY:
        run_scripts = [runPostsh_name]
    elif NO_POST:
        run_scripts = [runFSsh_name]
    else:
        run_scripts = [runFSsh_name,runPostsh_name]

    return run_scripts

def write_run_scripts(script_names, run_dir, output_dir, FS_option, prefix):
    """
    Write Freesurfer-DTI run scripts for this project if they don't
    already exist.
    """
    for name in script_names:
        runsh = os.path.join(run_dir, name)
        if os.path.isfile(runsh):
            ## create temporary run file and test it against the original
            check_runsh(runsh, output_dir, FS_option, prefix)
        else:
            ## if it doesn't exist, write it now
            make_Freesurfer_runsh(runsh, output_dir, FS_option, prefix)

def check_runsh(old_script, output_dir, FS_option, prefix):
    """
    Writes a temporary (run.sh) file and then checks it against
    the existing run script.

    This is used to double check that the pipeline is not being called
    with different options
    """
    with make_temp_directory() as temp_dir:
        tmp_runsh = os.path.join(temp_dir, os.path.basename(old_script))
        make_Freesurfer_runsh(tmp_runsh, output_dir, FS_option, prefix)
        if filecmp.cmp(old_script, tmp_runsh):
            logger.debug("{} already written - using it".format(old_script))
        else:
            # If the two files differ - then we use difflib package
            # to log differences
            logger.debug('#############################################################\n')
            logger.debug('# Found differences in {} these are marked with (+) '.format(old_script))
            logger.debug('#############################################################')
            with open(old_script) as f1, open(tmp_runsh) as f2:
                differ = difflib.Differ()
                logger.debug(''.join(differ.compare(f1.readlines(), f2.readlines())))
            logger.error("\nOld {} doesn't match parameters of this run." \
                         "...Exiting".format(old_script))
            sys.exit(1)

def make_Freesurfer_runsh(file_name, output_dir, FS_option, prefix):
    """
    Builds a Freesurfer-DTI run script
    """
    bname = os.path.basename(file_name)

    with open(file_name,'w') as Freesurfersh:
        Freesurfersh.write('#!/bin/bash\n\n')
        Freesurfersh.write('export SUBJECTS_DIR=' + output_dir + '\n\n')
        Freesurfersh.write('## Prints loaded modules to the log\nmodule list\n\n')

        if not 'postfreesurfer' in bname:
            ## Write a FS run script
            Freesurfersh.write('SUBJECT=${1}\n')
            Freesurfersh.write('shift\n')
            Freesurfersh.write('T1MAPS=${@}\n')
            Freesurfersh.write('\nrecon-all -all ')
            if FS_option is not None:
                Freesurfersh.write(FS_option + ' ')
            Freesurfersh.write('-subjid ${SUBJECT} ${T1MAPS}' + ' -qcache\n')
        else:
            ## Write a Post FS run script
            Freesurfersh.write('ENGIMA_ExtractCortical.sh ${SUBJECTS_DIR} '+ prefix + '\n')
            Freesurfersh.write('ENGIMA_ExtractSubcortical.sh ${SUBJECTS_DIR} '+ prefix + '\n')

    os.chmod(file_name, 0o755)

def subject_previously_completed(output_dir, subject):
    FS_completed = os.path.join(output_dir, subject, 'scripts', 'recon-all.done')
    if os.path.isfile(FS_completed):
        return True
    return False

def make_FS_command(run_dir, sh_name, job_name_prefix, log_dir, wall_time, subid=None, T1s=None):
    if subid is not None:
        # make FS command for subject
        job_command = "bash -l {}/{} {} {}".format(run_dir, sh_name,
                subid, ' '.join(T1s))
        job_name = job_name_prefix + subid
        cmd = dm.proc.make_piped_qbatch_command(job_command, job_name, log_dir,
                wall_time)
    else:
        # make post FS command
        job_command = 'bash -l {}/{}'.format(run_dir, sh_name)
        job_name = job_name_prefix + 'post'
        hold = job_name_prefix + '*'
        cmd = dm.proc.make_piped_qbatch_command(job_command, job_name, log_dir,
                wall_time, afterok=hold)
    return cmd

@contextlib.contextmanager
def make_temp_directory():
    temp_dir = tempfile.mkdtemp()
    try:
        yield temp_dir
    finally:
        shutil.rmtree(temp_dir)

### Erin's little function for running things in the shell
def docmd(cmd):
    "sends a command (inputed as a list) to the shell"
    logger.debug("Running command {}".format(cmd))
    rtn, out, err = dm.utils.run(cmd, dryrun = DRYRUN)

if __name__ == '__main__':
    main()
