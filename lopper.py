#!/usr/bin/env python3

#/*
# * Copyright (c) 2019,2020 Xilinx Inc. All rights reserved.
# *
# * Author:
# *       Bruce Ashfield <bruce.ashfield@xilinx.com>
# *
# * SPDX-License-Identifier: BSD-3-Clause
# */

import struct
import sys
import types
import unittest
import os
import getopt
import re
import subprocess
import shutil
from pathlib import Path
from pathlib import PurePath
from io import StringIO
import contextlib
import importlib
from importlib.machinery import SourceFileLoader
import tempfile
from enum import Enum
import atexit
import textwrap
from collections import UserDict
from collections import OrderedDict

import humanfriendly

from lopper_fdt import Lopper
from lopper_fdt import LopperFmt
from lopper_tree import LopperNode, LopperTree, LopperTreePrinter, LopperProp

import lopper_rest

try:
    from lopper_yaml import *
    yaml_support = True
except Exception as e:
    print( "[WARNING]: cant load yaml, disabling support: %s" % e )
    yaml_support = False

import lopper_tree

LOPPER_VERSION = "2020.4-beta"

lopper_directory = os.path.dirname(os.path.realpath(__file__))

@contextlib.contextmanager
def stdoutIO(stdout=None):
    old = sys.stdout
    if stdout is None:
        stdout = StringIO()
        sys.stdout = stdout
        yield stdout
        sys.stdout = old

def at_exit_cleanup():
    if device_tree:
        device_tree.cleanup()
    else:
        pass

class LopperAssist:
    """Internal class to contain the details of a lopper assist

    """
    def __init__(self, lop_file, module = "", properties_dict = {}):
        self.module = module
        self.file = lop_file
        # holds specific key,value properties
        self.properties = properties_dict

class LopperSDT:
    """The LopperSDT Class represents and manages the full system DTS file

    In particular this class:
      - wraps a dts/dtb/fdt containing a system description
      - Has a LopperTree representation of the system device tree
      - manages and applies operations to the tree
      - calls modules and assist functions for processing of that tree

    Attributes:
      - dts (string): the source device tree file
      - dtb (blob): the compiled dts
      - FDT (fdt): the primary flattened device tree represention of the dts
      - lops (list): list of loaded lopper operations
      - verbose (int): the verbosity level of operations
      - tree (LopperTree): node/property representation of the system device tree
      - dry_run (bool): whether or not changes should be written to disk
      - output_file (string): default output file for writing

    """
    def __init__(self, sdt_file):
        self.dts = sdt_file
        self.dtb = ""
        self.lops = []
        self.verbose = 0
        self.dry_run = False
        self.assists = []
        self.output_file = ""
        self.cleanup_flag = True
        self.save_temps = False
        self.enhanced = False
        self.FDT = None
        self.tree = None
        self.subtrees = {}
        self.outdir = "./"
        self.target_domain = ""
        self.load_paths = []
        self.permissive = False

    def setup(self, sdt_file, input_files, include_paths, force=False):
        """executes setup and initialization tasks for a system device tree

        setup validates the inputs, and calls the appropriate routines to
        preprocess and compile passed input files (.dts).

        Args:
           sdt_file (String): system device tree path/file
           input_files (list): list of input files (.dts, or .dtb) in addition to the sdt_file
           include_paths (list): list of paths to search for files
           force (bool,optional): flag indicating if files should be overwritten and compilation
                                  forced. Default is False.

        Returns:
           Nothing

        """
        if self.verbose:
            print( "[INFO]: loading dtb and using libfdt to manipulate tree" )

        # check for required support applications
        support_bins = ["dtc", "cpp" ]
        for s in support_bins:
            if self.verbose:
                print( "[INFO]: checking for support binary: %s" % s )
            if not shutil.which(s):
                print( "[ERROR]: support application '%s' not found, exiting" % s )
                sys.exit(2)

        self.use_libfdt = True

        current_dir = os.getcwd()

        lop_files = []
        sdt_files = []
        for ifile in input_files:
            if re.search( ".dts$", ifile ):
                # an input file is either a lopper operation file, or part of the
                # system device tree. We can check for compatibility to decide which
                # it is.
                with open(ifile) as f:
                    datafile = f.readlines()
                    found = False
                    for line in datafile:
                        if not found:
                            if re.search( "system-device-tree-v1,lop", line ):
                                lop_files.append( ifile )
                                found = True

                if not found:
                    sdt_files.append( ifile )
            elif re.search( ".dtb$", ifile ):
                lop_files.append( ifile )
            elif re.search( ".yaml$", ifile ):
                if yaml_support:
                    with open(ifile) as f:
                        datafile = f.readlines()
                        found = False
                        for line in datafile:
                            if not found:
                                if re.search( "system-device-tree-v1,lop", line ):
                                    lop_files.append( ifile )
                                    found = True

                    if not found:
                        sdt_files.append( ifile )
                else:
                    print( "[ERROR]. YAML support is not loaded, check dependencies" )
                    sys.exit(1)

        # is the sdt a dts ?
        sdt_extended_trees = []
        if re.search( ".dts$", self.dts ):
            # do we have any extra sdt files to concatenate first ?
            fp = ""
            fpp = tempfile.NamedTemporaryFile( delete=False )
            # TODO: if the count is one, we shouldn't be doing the tmp file processing.
            if sdt_files:
                sdt_files.insert( 0, self.dts )

                # this block concatenates all the files into a single dts to
                # compile
                with open( fpp.name, 'wb') as wfd:
                    for f in sdt_files:
                        if re.search( ".dts$", f ):
                            with open(f,'rb') as fd:
                                shutil.copyfileobj(fd, wfd)

                        elif re.search( ".yaml$", f ):
                            # look for a special front end, for this or any file for that matter
                            yaml = LopperYAML( f )
                            yaml_tree = yaml.to_tree()

                            # save the tree for future processing (and joining with the main
                            # system device tree). No code after this needs to be concerned that
                            # this came from yaml.
                            sdt_extended_trees.append( yaml_tree )

                fp = fpp.name
            else:
                sdt_files.append( sdt_file )
                fp = sdt_file

            # note: input_files isn't actually used by dt_compile, otherwise, we'd need to
            #       filter out non-dts files before the call .. we should probably still do
            #       that.
            self.dtb = Lopper.dt_compile( fp, input_files, include_paths, force, self.outdir,
                                          self.save_temps, self.verbose, self.enhanced )

            self.FDT = Lopper.dt_to_fdt(self.dtb, 'rb')

            # we export the compiled fdt to a dictionary, and load it into our tree
            dct = Lopper.export( self.FDT )
            self.tree = LopperTree()
            self.tree.load( dct )

            self.tree.strict = not self.permissive

            # join any extended trees to the one we just created
            for t in sdt_extended_trees:
                for node in t:
                    if node.abs_path != "/":
                        # old: deep copy the node
                        # new_node = node()
                        # assign it to the main system device tree
                        self.tree = self.tree + node

            fpp.close()
        elif re.search( ".yaml$", self.dts ):
            if not yaml_support:
                print( "[ERROR]: no yaml support detected, but system device tree is yaml" )
                sys.exit(1)

            fp = ""
            fpp = tempfile.NamedTemporaryFile( delete=False )
            if sdt_files:
                sdt_files.insert( 0, self.dts )

                # this block concatenates all the files into a single yaml file to process
                with open( fpp.name, 'wb') as wfd:
                    for f in sdt_files:
                        with open(f,'rb') as fd:
                            shutil.copyfileobj(fd, wfd)

                fp = fpp.name
            else:
                sdt_files.append( sdt_file )
                fp = sdt_file

            yaml = LopperYAML( fp )
            lt = yaml.to_tree()

            # temp location. check to see if automatic translations are
            # registered for the intput file type, and generate the lops

            # or .. is this really input type necessary ?!

            self.dtb = None
            self.FDT = Lopper.fdt()
            self.tree = lt
        else:
            # the system device tree is a dtb
            self.dtb = sdt_file
            self.dts = sdt_file
            self.FDT = Lopper.dt_to_fdt(self.dtb, 'rb')
            self.tree = LopperTree()
            self.tree.load( Lopper.export( self.FDT ) )
            self.tree.strict = not self.permissive

        if self.verbose:
            print( "" )
            print( "SDT summary:")
            print( "   system device tree: %s" % sdt_files )
            print( "   lops: %s" % lop_files )
            print( "   output: %s" % self.output_file )
            print( "" )

        # Individually compile the input files. At some point these may be
        # concatenated with the main SDT if dtc is doing some of the work, but for
        # now, libfdt is doing the transforms so we compile them separately
        for ifile in lop_files:
            if re.search( ".dts$", ifile ):
                lop = LopperFile( ifile )
                # TODO: this may need an output directory option, right now it drops
                #       it where lopper is called from (which may not be writeable.
                #       hence why our output_dir is set to "./"
                compiled_file = Lopper.dt_compile( lop.dts, "", include_paths, force, self.outdir,
                                                   self.save_temps, self.verbose )
                if not compiled_file:
                    print( "[ERROR]: could not compile file %s" % ifile )
                    sys.exit(1)
                lop.dtb = compiled_file
                self.lops.append( lop )
            elif re.search( ".yaml$", ifile ):
                yaml = LopperYAML( ifile )
                yaml_tree = yaml.to_tree()

                lop = LopperFile( ifile )
                lop.dts = ""
                lop.dtb = ""
                lop.fdt = None
                lop.tree = yaml_tree
                self.lops.append( lop )
            elif re.search( ".dtb$", ifile ):
                lop = LopperFile( ifile )
                lop.dts = ""
                lop.dtb = ifile
                self.lops.append( lop )

    def assists_setup( self, assists = []):
        """
                   assists (list,optional): list of python assist modules to load. Default is []
        """
        for a in assists:
            a_file = self.assist_find( a )
            if a_file:
                self.assists.append( LopperAssist( str(a_file.resolve()) ) )

        self.assists_wrap()

    def assist_autorun_setup( self, module_name, module_args = [] ):
        lt = LopperTree()

        lt['/']['compatible'] = [ 'system-device-tree-v1' ]
        lt['/']['priority'] = [ 3 ]

        ln = LopperNode()
        ln.name = "lops"

        mod_count = 0
        lop_name = "lop_{}".format( mod_count )

        lop_node = LopperNode()
        lop_node.name = lop_name
        lop_node['compatible'] = [ 'system-device-tree-v1,lop,assist-v1' ]
        lop_node['node'] = [ '/' ]

        if module_args:
            module_arg_string = ""
            for m in module_args:
                module_arg_string = module_arg_string + " " + m
                lop_node['options'] = [ module_arg_string ]

        lop_node['id'] = [ "module," + module_name ]

        ln = ln + lop_node
        lt = lt + ln

        lop = LopperFile( 'commandline' )
        lop.dts = ""
        lop.dtb = ""
        lop.fdt = None
        lop.tree = lt

        if self.verbose > 1:
            print( "[INFO]: generated assist run for %s" % module_name )

        self.lops.insert( 0, lop )

    def cleanup( self ):
        """cleanup any temporary or copied files

        Either called directly, or registered as an atexit handler. Any
        temporary or copied files are removed, as well as other relevant
        cleanup.

        Args:
           None

        Returns:
           Nothing

        """
        # remove any .dtb and .pp files we created
        if self.cleanup and not self.save_temps:
            try:
                if self.dtb != self.dts:
                    os.remove( self.dtb )
                if self.enhanced:
                    os.remove( self.dts + ".enhanced" )
            except:
                # doesn't matter if the remove failed, it means it is
                # most likely gone
                pass

        # note: we are not deleting assists .dtb files, since they
        #       can actually be binary blobs passed in. We are also
        #       not cleaning up the concatenated compiled. pp file, since
        #       it is created with mktmp()

    def write( self, fdt = None, output_filename = None, overwrite = True, enhanced = False ):
        """Write a system device tree to a file

        Write a fdt (or system device tree) to an output file. This routine uses
        the output filename to determine if a module should be used to write the
        output.

        If the output format is .dts or .dtb, Lopper takes care of writing the
        output. If it is an unrecognized output type, the available assist
        modules are queried for compatibility. If there is a compatible assist,
        it is called to write the file, otherwise, a warning or error is raised.

        Args:
            fdt (fdt,optional): source flattened device tree to write
            output_filename (string,optional): name of the output file to create
            overwrite (bool,optional): Should existing files be overwritten. Default is True.
            enhanced(bool,optional): whether enhanced printing should be performed. Default is False

        Returns:
            Nothing

        """
        if not output_filename:
            output_filename = self.output_file

        if not output_filename:
            return

        fdt_to_write = fdt
        if not fdt_to_write:
            fdt_to_write = self.FDT

        if re.search( ".dtb", output_filename ):
            Lopper.write_fdt( fdt_to_write, output_filename, overwrite, self.verbose )

        elif re.search( ".dts", output_filename ):
            if enhanced:
                o = Path(output_filename)
                if o.exists() and not overwrite:
                    print( "[ERROR]: output file %s exists and force overwrite is not enabled" % output_filename )
                    sys.exit(1)

                printer = LopperTreePrinter( True, output_filename, self.verbose )
                printer.strict = not self.permissive

                # Note: the caller must ensure that all changes have been sync'd to
                #        the fdt_to_write.

                printer.load( Lopper.export( fdt_to_write ) )
                printer.exec()
            else:
                Lopper.write_fdt( fdt_to_write, output_filename, overwrite, self.verbose, False )

        elif re.search( ".yaml", output_filename ):
            o = Path(output_filename)
            if o.exists() and not overwrite:
                print( "[ERROR]: output file %s exists and force overwrite is not enabled" % output_filename )
                sys.exit(1)

            yaml = LopperYAML( None, self.tree )
            yaml.to_yaml( output_filename )
        else:
            # we use the outfile extension as a mask
            (out_name, out_ext) = os.path.splitext(output_filename)
            cb_funcs = self.find_compatible_assist( 0, "", out_ext )
            if cb_funcs:
                for cb_func in cb_funcs:
                    try:
                        out_tree = LopperTreePrinter( True, output_filename, self.verbose )
                        Lopper.sync( fdt_to_write, self.tree.export() )
                        out_tree.load( Lopper.export( fdt_to_write ) )
                        out_tree.strict = not self.permissive
                        if not cb_func( 0, out_tree, { 'outfile': output_filename, 'verbose' : self.verbose } ):
                            print( "[WARNING]: output assist returned false, check for errors ..." )
                    except Exception as e:
                        print( "[WARNING]: output assist %s failed: %s" % (cb_func,e) )
                        exc_type, exc_obj, exc_tb = sys.exc_info()
                        fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
                        print(exc_type, fname, exc_tb.tb_lineno)
                        if self.werror:
                            sys.exit(1)
            else:
                if self.verbose:
                    print( "[INFO]: no compatible output assist found, skipping" )
                if self.werror:
                    print( "[ERROR]: werror is enabled, and no compatible output assist found, exiting" )
                    sys.exit(2)

    def assist_find(self, assist_name, local_load_paths = []):
        """Locates a python module that matches assist_name

        This routine searches both system (lopper_directory, lopper_directory +
        "assists", and passed paths (local_load_paths) to locate a matching
        python implementation.

        Args:
           assist_name (string): name of the assist to locate
           local_load_paths (list of strings, optional): list of directories to search
                                                         in addition to system dirs

        Returns:
           Path: Path object to the located python module, None on failure

        """
        mod_file = Path( assist_name )
        mod_file_wo_ext = mod_file.with_suffix('')

        if self.verbose > 1:
            print( "[DBG+]: assist_find: %s local search: %s" % (assist_name,local_load_paths) )


        # anything less than python 3.6.x doesn't take "true" as a parameter to
        # resolve. So we make it conditional on the version.

        try:
            if sys.version_info.minor < 6:
                mod_file_abs = mod_file.resolve()
            else:
                mod_file_abs = mod_file.resolve( True )
            if not mod_file_abs:
                raise FileNotFoundError( "Unable to find assist: %s" % mod_file )
        except FileNotFoundError:
            # check the path from which lopper is running, that directory + assists, and paths
            # specified on the command line
            search_paths =  [ lopper_directory ] + [ lopper_directory + "/assists/" ] + local_load_paths
            for s in search_paths:
                mod_file = Path( s + "/" + mod_file.name )
                try:
                    if sys.version_info.minor < 6:
                        mod_file_abs = mod_file.resolve()
                    else:
                        mod_file_abs = mod_file.resolve( True )
                    if not mod_file_abs:
                        raise FileNotFoundError( "Unable to find assist: %s" % mod_file )
                except FileNotFoundError:
                    mod_file_abs = ""

                if not mod_file_abs and not mod_file.name.endswith( ".py"):
                    # try it with a .py
                    mod_file = Path( s + "/" + mod_file.name + ".py" )
                    try:
                        if sys.version_info.minor < 6:
                            mod_file_abs = mod_file.resolve()
                        else:
                            mod_file_abs = mod_file.resolve( True )
                        if not mod_file_abs:
                            raise FileNotFoundError( "Unable to find assist: %s" % mod_file )
                    except FileNotFoundError:
                        mod_file_abs = ""


            if not mod_file_abs:
                print( "[ERROR]: module file %s not found" % assist_name )
                if self.werror:
                    sys.exit(1)
                return None

        return mod_file

    def assists_wrap(self):
        """wrap assists that have been added to the device tree

        Wraps any command line assists that have been added to the system
        device tree. A standard lop format dtb is generated for any found
        assists, such that they will be loaded in the same manner as
        assists passed directly in lop files.

        Note: this is for internal use only

        Args:
           None

        Returns:
           Nothing

        """
        if self.assists:
            lt = LopperTree()

            lt['/']['compatible'] = [ 'system-device-tree-v1' ]
            lt['/']['priority'] = [ 1 ]

            ln = LopperNode()
            ln.name = "lops"

            assist_count = 0
            for a in set(self.assists):
                lop_name = "lop_{}".format( assist_count )

                lop_node = LopperNode()
                lop_node.name = lop_name
                lop_node['compatible'] = [ 'system-device-tree-v1,lop,load' ]
                lop_node['load'] = [ a.file ]

                ln = ln + lop_node

                if self.verbose > 1:
                    print( "[INFO]: generated load lop for assist %s" % a )

                assist_count = assist_count + 1

            lt = lt + ln

            lop = LopperFile( 'commandline' )
            lop.dts = ""
            lop.dtb = ""
            lop.fdt = None
            lop.tree = lt

            self.lops.insert( 0, lop )

    def domain_spec(self, tgt_domain, tgt_domain_id = "openamp,domain-v1"):
        """generate a lop for a command line passed domain

        When a target domain is passed on the command line, we must generate
        a lop dtb for it, so that it can be processed along with other
        operations

        Args:
           tgt_domain (string): path to the node to use as the domain
           tgt_domain_id (string): assist identifier to use for locating a
                                   registered assist.

        Returns:
           Nothing

        """
        # This is called from the command line. We need to generate a lop
        # device tree with:
        #
        # lop_0 {
        #     compatible = "system-device-tree-v1,lop,assist-v1";
        #     node = "/chosen/openamp_r5";
        #     id = "openamp,domain-v1";
        # };
        # and then inject it into self.lops to run first


        lt = LopperTree()

        lt['/']['compatible'] = [ 'system-device-tree-v1' ]
        lt['/']['priority'] = [ 3 ]

        ln = LopperNode()
        ln.name = "lops"

        mod_count = 0
        lop_name = "lop_{}".format( mod_count )

        lop_node = LopperNode()
        lop_node.name = lop_name
        lop_node['compatible'] = [ 'system-device-tree-v1,lop,assist-v1' ]
        lop_node['id'] = [ tgt_domain_id ]

        ln = ln + lop_node
        lt = lt + ln

        lop = LopperFile( 'commandline' )
        lop.dts = ""
        lop.dtb = ""
        lop.fdt = None
        lop.tree = lt

        self.lops.insert( 0, lop )

    def find_compatible_assist( self, cb_node = None, cb_id = "", mask = "" ):
        """Finds a registered assist that is compatible with a given ID

        Searches the registered assists for one that is compatible with an ID.

        The is_compat() routine is called for each registered module. If an
        assist is capabable of handling a given ID, it returns True and
        associated actions can then be taken.

        I addition to an ID string, a mask can optionally be provided to this
        routine. Any assists that have registered a mask, will have that
        checked, before calling the is_compat() routine. This allows assists to
        be generically registered, but filtered by the caller rather than only
        their is_compat() routines.

        Args:
            cb_node (int,optional): node offset to be tested. Default is 0 (root)
            cb_id (string,optional): ID to be tested for compatibility. Default is ""
            mask (string,optional): caller mask for filtering nodes. Default is ""

        Returns:
            function reference: the callback routine, or "", if no compatible routine found

        """
        # default for cb_node is "start at root (0)"
        cb_func = []
        if self.assists:
            for a in self.assists:
                if a.module:
                    # if the passed id is empty, check to see if the assist has
                    # one as part of its data
                    if not cb_id:
                        try:
                            cb_id = a.properties['id']
                        except:
                            cb_id = ""

                    # if a non zero mask was passed, and the module has a mask, make
                    # sure they match before even considering it.
                    mask_ok = True
                    try:
                        assist_mask = a.properties['mask']
                    except:
                        assist_mask = ""

                    if mask and assist_mask:
                        mask_ok = False
                        # TODO: could be a regex
                        if mask == assist_mask:
                            mask_ok = True

                    if mask_ok:
                        cb_f = a.module.is_compat( cb_node, cb_id )

                    if cb_f:
                        cb_func.append( cb_f )
                        # we could double check that the function exists with this call:
                        #    func = getattr( m, cbname )
                        # but for now, we don't
                else:
                    print( "[WARNING]: a configured assist has no module loaded" )
        else:
            print( "[WARNING]: no modules loaded, no compat search is possible" )

        return cb_func

    def exec_lop( self, lop_node, lops_tree, options = None ):
        """Executes a a lopper operation (lop)

        Runs a lopper operation against the system device tree.

        Details of the lop are in the lops_fdt, with extra parameters and lop
        specific information from the caller being passed in the options
        variable.

        Args:
            lops_fdt (FDT): lopper operation flattened device tree
            lop_node_number (int): node number for the operation in lops_fdt
            options (dictionary,optional): lop specific options passed from the caller

        Returns:
            boolean

        """

        # TODO: stop using this and go to the searching in the lops processing loop.
        lop_type = lop_node['compatible'].value[0]
        # TODO: lop_args is really a "subtype"
        try:
            lop_args = lop_node['compatible'].value[1]
        except:
            lop_args = ""

        if self.verbose > 1:
            print( "[DBG++]: executing lop: %s" % lop_type )

        if re.search( ".*,exec.*$", lop_type ):
            if self.verbose > 1:
                print( "[DBG++]: code exec jump" )
            try:
                try:
                    node_spec = lop_node['node'].value[0]
                except:
                    if self.tree.__selected__:
                        node_spec = self.tree.__selected__[0]
                    else:
                        node_spec = ""

                if not options:
                    options = {}

                try:
                    options_spec = lop_node['options'].value
                except:
                    options_spec = ""

                if options_spec:
                    for o in options_spec:
                        opt_key,opt_val = o.split(":")
                        if opt_key:
                            options[opt_key] = opt_val

                exec_tgt = lop_node['exec'].value[0]
                target_node = lops_tree.pnode( exec_tgt )
                if self.verbose > 1:
                    print( "[DBG++]: exec phandle: %s target: %s" % (exec_tgt,target_node))

                if target_node:
                    try:
                        if node_spec:
                            options['start_node'] = node_spec

                        ret = self.exec_lop( target_node, lops_tree, options )
                    except Exception as e:
                        print( "[WARNING]: exec block caused exception: %s" % e )
                        ret = False

                    return ret
                else:
                    return False

            except Exception as e:
                print( "[WARNING]: exec lop exception: %s" % e )
                return False

        if re.search( ".*,print.*$", lop_type ):
            print_props = lop_node.props('print.*')
            for print_prop in print_props:
                for line in print_prop.value:
                    if type(line) == str:
                        print( line )
                    else:
                        # is it a phandle?
                        node = self.tree.pnode(line)
                        if node:
                            print( "%s {" % node )
                            for p in node:
                                print( "    %s" % p )
                            print( "}" )

        if re.search( ".*,select.*$", lop_type ):
            select_props = lop_node.props( 'select.*' )

            try:
                tree_name = lop_node['tree'].value[0]
                try:
                    tree = self.subtrees[tree_name]
                except:
                    print( "[ERROR]: tree name provided (%s), but not found" % tree_name )
                    sys.exit(1)
            except:
                tree = self.tree

            #
            # to do an "or" condition
            #    select_1 = "/path/or/regex/to/nodes:prop:val";
            #    select_2 = "/path/or/2nd/regex:prop2:val2";
            #
            # to do an "and" condition:
            #    select_1 = "/path/or/regex/to/nodes:prop:val";
            #    select_2 = ":prop2:val2";
            #
            selected_nodes = []
            selected_nodes_possible = []
            for sel in select_props:
                if sel.value == ['']:
                    if self.verbose > 1:
                        print( "[DBG++]: clearing selected nodes" )
                    tree.__selected__ = []
                else:
                    # if different node regex + properties are listed in the same
                    # select = "foo","bar","blah", they are always AND conditions.
                    for s in sel.value:
                        if self.verbose > 1:
                            print( "[DBG++]: running node selection: %s (%s)" % (s,selected_nodes_possible) )
                        try:
                            node_regex, prop, prop_val = s.split(":")
                        except:
                            node_regex = s
                            prop = ""
                            prop_val = ""

                        if node_regex:
                            if selected_nodes_possible:
                                selected_nodes_possible = selected_nodes_possible + tree.nodes( node_regex )
                            else:
                                selected_nodes_possible = tree.nodes( node_regex )
                        else:
                            # if the node_regex is empty, we operate on previously
                            # selected nodes.
                            if selected_nodes:
                                selected_nodes_possible = selected_nodes
                            else:
                                selected_nodes_possible = tree.__selected__

                        if self.verbose > 1:
                            print( "[DBG++]: selected potential nodes:" )
                            for n in selected_nodes_possible:
                                print( "       %s" % n )

                        if prop and prop_val:
                            invert_result = False
                            if re.search( "\!", prop_val ):
                                invert_result = True
                                prop_val = re.sub( '^\!', '', prop_val )
                                if self.verbose > 1:
                                    print( "[DBG++]: select: inverting result" )

                            # in case this is a formatted list, ask lopper to convert
                            prop_val = Lopper.property_convert( prop_val )

                            # construct a test prop, so we can use the internal compare
                            test_prop = LopperProp( prop, -1, None, prop_val )
                            test_prop.ptype = test_prop.property_type_guess( True )

                            # we need this list(), since the removes below will yank items out of
                            # our iterator if we aren't careful
                            for sl in list(selected_nodes_possible):
                                try:
                                    sl_prop = sl[prop]
                                except Exception as e:
                                    sl_prop = None
                                    are_they_equal = False

                                if sl_prop:
                                    if self.verbose > 2:
                                        test_prop.__dbg__ = self.verbose

                                    are_they_equal = test_prop.compare( sl_prop )
                                    if invert_result:
                                        are_they_equal = not are_they_equal

                                    if are_they_equal:
                                        if not sl in selected_nodes:
                                            selected_nodes.append( sl )
                                    else:
                                        # no match, you are out! (only if this is an AND operation though, which
                                        # is indicated by the lack of a node regex)
                                        if not node_regex:
                                            if sl in selected_nodes:
                                                selected_nodes.remove( sl )
                                else:
                                    # no prop, you are out! (only if this is an AND operation though, which
                                    # is indicated by the lack of a node regex)
                                    if not node_regex:
                                        if sl in selected_nodes:
                                            selected_nodes.remove( sl )

                        if prop and not prop_val:
                            # an empty property value means we are testing if the property exists

                            # if the property name is "!<property>" and the val is empty, then we
                            # are testing if it doesn't exist.

                            prop_exists_test = True
                            if re.search( "\!", prop ):
                                prop_exists_test = False

                            # remove any leading '!' from the name.
                            prop = re.sub( '^\!', '', prop )

                            for sl in list(selected_nodes_possible):
                                try:
                                    sl_prop = sl[prop]
                                except Exception as e:
                                    sl_prop = None

                                if prop_exists_test:
                                    if sl_prop:
                                        if not sl in selected_nodes:
                                            selected_nodes.append( sl )
                                    else:
                                        if sl in selected_nodes:
                                            selected_nodes.remove( sl )
                                else:
                                    # we are looking for the *lack* of a property
                                    if sl_prop:
                                        if sl in selected_nodes:
                                            selected_nodes.remove( sl )
                                    else:
                                        if not sl in selected_nodes:
                                            selected_nodes.append( sl )

                        if not prop and not prop_val:
                            selected_nodes = selected_nodes_possible


                    if self.verbose > 1:
                        print( "[DBG++]: selected nodes:" )
                        for n in selected_nodes:
                            print( "    %s" % n )

            # update the tree selection with our results
            tree.__selected__ = selected_nodes

        if re.search( ".*,meta.*$", lop_type ):
            if re.search( "phandle-desc", lop_args ):
                if self.verbose > 1:
                    print( "[DBG++]: processing phandle meta data" )
                Lopper.phandle_possible_prop_dict = OrderedDict()
                for p in lop_node:
                    # we skip compatible, since that is actually the compatibility value
                    # of the node, not a meta data entry. Everything else is though
                    if p.name != "compatible":
                        Lopper.phandle_possible_prop_dict[p.name] = [ p.value[0] ]

        if re.search( ".*,output$", lop_type ):
            try:
                output_file_name = lop_node['outfile'].value[0]
            except:
                print( "[ERROR]: cannot get output file name from lop" )
                sys.exit(1)

            if self.verbose > 1:
                print( "[DBG+]: outfile is: %s" % output_file_name )

            try:
                tree_name = lop_node['tree'].value[0]
                try:
                    tree = self.subtrees[tree_name]
                except:
                    print( "[ERROR]: tree name provided (%s), but not found" % tree_name )
                    sys.exit(1)
            except:
                tree = self.tree


            output_nodes = []
            try:
                output_regex = lop_node['nodes'].value
            except:
                output_regex = []

            if not output_regex:
                if tree.__selected__:
                    output_nodes = tree.__selected__

            if not output_regex and not output_nodes:
                return False

            if self.verbose > 1:
                print( "[DBG+]: output regex: %s" % output_regex )

            output_tree = None
            if output_regex:
                output_nodes = []
                # select some nodes!
                if "*" in output_regex:
                    output_tree = LopperTree( True )
                    output_tree.load( tree.export() )
                    output_tree.strict = not self.permissive
                else:
                    # we can gather the output nodes and unify with the selected
                    # copy below.
                    for regex in output_regex:
                        split_node = regex.split(":")
                        o_node_regex = split_node[0]
                        o_prop_name = ""
                        o_prop_val = ""
                        if len(split_node) > 1:
                            o_prop_name = split_node[1]
                            if len(split_node) > 2:
                                o_prop_val = split_node[2]

                        # Note: we may want to switch this around, and copy the old tree and
                        #       delete nodes. This will be important if we run into some
                        #       strangely formatted ones that we can't copy.

                        try:
                            # if there's no / anywhere in the regex, then it is just
                            # a node name, and we need to wrap it in a regex. This is
                            # for compatibility with when just node names were allowed
                            c = re.findall( '/', o_node_regex )
                            if not c:
                                o_node_regex = ".*" + o_node_regex

                            o_nodes = tree.nodes(o_node_regex)
                            if not o_nodes:
                                # was it a label ?
                                label_nodes = []
                                try:
                                    o_nodes = tree.lnodes(o_node_regex)
                                except Exception as e:
                                    pass

                            for o in o_nodes:
                                if self.verbose > 2:
                                    print( "[DBG++] output lop, checking node: %s" % o.abs_path )

                                # we test for a property in the node if it was defined
                                if o_prop_name:
                                    p = tree[o].propval(o_prop_name)
                                    if o_prop_val:
                                        if p:
                                            if o_prop_val in p:
                                                if not o in output_nodes:
                                                    output_nodes.append( o )
                                else:
                                    if not o in output_nodes:
                                        output_nodes.append( o )

                        except Exception as e:
                            print( "[WARNING]: except caught during output processing: %s" % e )

                if output_regex:
                    if self.verbose > 2:
                        print( "[DBG++] output lop, final nodes:" )
                        for oo in output_nodes:
                            print( "       %s" % oo.abs_path )

                if not output_tree and output_nodes:
                    output_tree = LopperTreePrinter()
                    output_tree.strict = not self.permissive
                    output_tree.__dbg__ = self.verbose
                    for on in output_nodes:
                        # make a deep copy of the selected node
                        new_node = on()
                        new_node.__dbg__ = self.verbose
                        # and assign it to our tree
                        # if the performance of this becomes a problem, we can use
                        # direct calls to Lopper.node_copy_from_path()
                        output_tree + new_node

            if not self.dryrun:
                if output_tree:
                    output_file_full = self.outdir + "/" + output_file_name

                    # create a FDT
                    out_fdt = Lopper.fdt()
                    # export it
                    dct = output_tree.export()
                    Lopper.sync( out_fdt, dct )

                    # we should consider checking the type, and not doing the export
                    # if going to dts, since that is already easily done with the tree.
                    self.write( out_fdt, output_file_full, True, self.enhanced )
            else:
                print( "[NOTE]: dryrun detected, not writing output file %s" % output_file_name )

        if re.search( ".*,tree$", lop_type ):
            # TODO: consolidate this with the output lop
            try:
                tree_name = lop_node['tree'].value[0]
            except:
                print( "[ERROR]: tree lop: cannot get tree name from lop" )
                sys.exit(1)

            if self.verbose > 1:
                print( "[DBG+]: tree lop: tree is: %s" % tree_name )

            tree_nodes = []
            try:
                tree_regex = lop_node['nodes'].value
            except:
                tree_regex = []

            if not tree_regex:
                if self.tree.__selected__:
                    tree_nodes = self.tree.__selected__

            if not tree_regex and not tree_nodes:
                print( "[WARNING]: tree lop: no nodes or regex proviced for tree, returning" )
                return False

            new_tree = None
            if tree_regex:
                tree_nodes = []
                # select some nodes!
                if "*" in tree_regex:
                    new_tree = LopperTree( True )
                    new_tree.load( Lopper.export( self.FDT ) )
                    new_tree.strict = not self.permissive
                else:
                    # we can gather the tree nodes and unify with the selected
                    # copy below.
                    for regex in tree_regex:

                        split_node = regex.split(":")
                        o_node_regex = split_node[0]
                        o_prop_name = ""
                        o_prop_val = ""
                        if len(split_node) > 1:
                            o_prop_name = split_node[1]
                            if len(split_node) > 2:
                                o_prop_val = split_node[2]

                        # Note: we may want to switch this around, and copy the old tree and
                        #       delete nodes. This will be important if we run into some
                        #       strangely formatted ones that we can't copy.

                        try:
                            # if there's no / anywhere in the regex, then it is just
                            # a node name, and we need to wrap it in a regex. This is
                            # for compatibility with when just node names were allowed
                            c = re.findall( '/', o_node_regex )
                            if not c:
                                o_node_regex = ".*" + o_node_regex

                            o_nodes = self.tree.nodes(o_node_regex)
                            if not o_nodes:
                                # was it a label ?
                                label_nodes = []
                                try:
                                    o_nodes = self.tree.lnodes(o_node_regex)
                                except Exception as e:
                                    pass

                            for o in o_nodes:
                                # we test for a property in the node if it was defined
                                if o_prop_name:
                                    p = self.tree[o].propval(o_prop_name)
                                    if o_prop_val:
                                        if p:
                                            if o_prop_val in p:
                                                if not o in tree_nodes:
                                                    tree_nodes.append( o )
                                else:
                                    if not o in tree_nodes:
                                        tree_nodes.append( o )

                        except Exception as e:
                            print( "[WARNING]: except caught during tree processing: %s" % e )

                if not new_tree and tree_nodes:
                    new_tree = LopperTreePrinter()
                    new_tree.strict = not self.permissive
                    new_tree.__dbg__ = self.verbose
                    for on in tree_nodes:
                        # make a deep copy of the selected node
                        new_node = on()
                        new_node.__dbg__ = self.verbose
                        # and assign it to our tree
                        # if the performance of this becomes a problem, we can use
                        # direct calls to Lopper.node_copy_from_path()
                        new_tree + new_node

            if new_tree:
                self.subtrees[tree_name] = new_tree
            else:
                print( "[ERROR]: no tree created, exiting" )
                sys.exit(1)


        if re.search( ".*,assist-v1$", lop_type ):
            # also note: this assist may change from being called as
            # part of the lop loop, to something that is instead
            # called by walking the entire device tree, looking for
            # matching nodes and making assists at that moment.
            #
            # but that sort of node walking, will invoke the assists
            # out of order with other lopper operations, so it isn't
            # particularly feasible or desireable.
            #
            try:
                cb_tgt_node_name = lop_node['node'].value[0]
            except:
                print( "[ERROR]: cannot find target node for the assist" )
                sys.exit(1)

            try:
                cb = lop_node.propval('assist')[0]
                cb_id = lop_node.propval('id')[0]
                cb_opts = lop_node.propval('options')[0]
                cb_opts = cb_opts.lstrip()
                if cb_opts:
                    cb_opts = cb_opts.split( ' ' )
                else:
                    cb_opts = []
            except Exception as e:
                print( "[ERROR]: callback options are missing: %s" % e )
                sys.exit(1)

            try:
                cb_node = self.tree.nodes(cb_tgt_node_name )[0]
            except:
                cb_node = None

            if not cb_node:
                if self.werror:
                    print( "[ERROR]: cannot find assist target node in tree" )
                    sys.exit(1)
                else:
                    return

            if self.verbose:
                print( "[INFO]: assist lop detected" )
                if cb:
                    print( "        cb: %s" % cb )
                print( "        id: %s opts: %s" % (cb_id,cb_opts) )

            cb_funcs = self.find_compatible_assist( cb_node, cb_id )
            if cb_funcs:
                for cb_func in cb_funcs:
                    try:
                        if not cb_func( cb_node, self, { 'verbose' : self.verbose, 'args': cb_opts } ):
                            print( "[WARNING]: the assist returned false, check for errors ..." )
                    except Exception as e:
                        print( "[WARNING]: assist %s failed: %s" % (cb_func,e) )
                        exc_type, exc_obj, exc_tb = sys.exc_info()
                        fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
                        print(exc_type, fname, exc_tb.tb_lineno)
                        # exit if warnings are treated as errors
                        if self.werror:
                            sys.exit(1)
            else:
                if self.verbose:
                    print( "[INFO]: no compatible assist found, skipping: %s %s" % (cb_tgt_node_name,cb))

        if re.search( ".*,lop,load$", lop_type ):
            prop_id = ""
            prop_extension = ""

            try:
                load_prop = lop_node['load'].value[0]
            except:
                load_prop = ""

            if load_prop:
                # for submodule loading
                for p in self.load_paths:
                    if p not in sys.path:
                        sys.path.append( p )

                if self.verbose:
                    print( "[INFO]: loading module %s" % load_prop )

                mod_file = self.assist_find( load_prop, self.load_paths )
                if not mod_file:
                    print( "[ERROR]: unable to find assist (%s)" % load_prop )
                    sys.exit(1)

                mod_file_abs = mod_file.resolve()
                # append the directory of the located module onto the search
                # path. This is needed if that module imports something from
                # its own directory
                sys.path.append( str(mod_file_abs.parent) )
                try:
                    imported_module = SourceFileLoader( mod_file.name, str(mod_file_abs) ).load_module()
                except Exception as e:
                    print( "[ERROR]: could not load assist: %s: %s" % (mod_file_abs,e) )
                    sys.exit(1)

                assist_properties = {}
                try:
                    props = lop_node['props'].value
                except:
                    # does the module have a "props" routine for extra querying ?
                    try:
                        props = imported_module.props()
                    except:
                        props = []

                for p in props:
                    # TODO: we can generate and evaluate these generically, right now, this
                    #       is ok as a proof of concept only
                    if p == "file_ext":
                        try:
                            prop_extension = lop_node['file_ext'].value[0]
                        except:
                            try:
                                prop_extension = imported_module.file_ext()
                            except:
                                prop_extension = ""

                        assist_properties['mask'] = prop_extension

                    if p == "id":
                        try:
                            prop_id = lop_node['id'].value[0]
                        except:
                            try:
                                prop_id = imported_module.id()
                            except:
                                prop_id = ""

                        assist_properties['id'] = prop_id

                # TODO: move this "assist already available" check into a function
                already_loaded = False
                if self.assists:
                    for a in self.assists:
                        try:
                            if Path(a.file).resolve() == mod_file.resolve():
                                already_loaded = True
                                a.module = imported_module
                                a.properties = assist_properties
                        except:
                            pass
                if not already_loaded:
                    if self.verbose > 1:
                        if prop_extension:
                            print( "[INFO]: loading assist with properties (%s,%s)" % (prop_extension, prop_id) )

                    self.assists.append( LopperAssist( mod_file.name, imported_module, assist_properties ) )

        if re.search( ".*,lop,add$", lop_type ):
            if self.verbose:
                print( "[INFO]: node add lop" )

            try:
                src_node_name = lop_node['node_src'].value[0]
            except:
                print( "[ERROR]: node add detected, but no node name found" )
                sys.exit(1)

            try:
                tree_name = lop_node['tree'].value[0]
                try:
                    tree = self.subtrees[tree_name]
                except:
                    print( "[ERROR]: tree name provided (%s), but not found" % tree_name )
                    sys.exit(1)
            except:
                tree = self.tree


            lops_node_path = lop_node.abs_path
            src_node_path = lops_node_path + "/" + src_node_name

            try:
                dest_node_path = lop_node["node_dest"].value[0]
            except:
                dest_node_path = "/" + src_node_name

            if self.verbose:
                print( "[INFO]: add node name: %s node path: %s" % (src_node_path, dest_node_path) )


            if tree:
                src_node = lops_tree[src_node_path]

                # copy the source node
                dst_node = src_node()
                # adjust the path to where it will land
                dst_node.abs_path = dest_node_path

                # add it to the tree, and this will adjust the children appropriately
                tree + dst_node
            else:
                print( "[ERROR]: unable to copy node: %s" % src_node_name )
                sys.exit(1)

        if re.search( ".*,lop,conditional.*$", lop_type ):
            if self.verbose:
                print( "[INFO]: conditional lop found" )

            try:
                tree_name = lop_node['tree'].value[0]
                try:
                    tree = self.subtrees[tree_name]
                except:
                    print( "[ERROR]: tree name provided (%s), but not found" % tree_name )
                    sys.exit(1)
            except:
                tree = self.tree

            this_lop_subnodes = lop_node.subnodes()
            # the "cond_root" property of the lop node is the name of a node
            # under the same lop node that is the start of the conditional node
            # chain. If one wasn't provided, we start at '/'
            try:
                root = lop_node["cond_root"].value[0]
            except:
                root = "/"

            try:
                conditional_start = lops_tree[lop_node.abs_path + "/" + root]
            except:
                print( "[INFO]: conditional node %s not found, returning" % lop_node.abs_path + "/" + root )
                return False

            # the subnodes of the conditional lop represent the set of conditions
            # to use. The deepest node is what we'll be comparing
            cond_nodes = conditional_start.subnodes()
            # get the last node
            cond_last_node = cond_nodes[-1]
            # drop the path to the this conditional lop from the full path of
            # the last node in the chain. That's the path we'll look for in the
            # system device tree.
            cond_path = re.sub( lop_node.abs_path, "", cond_last_node.abs_path)

            sdt_tgt_nodes = tree.nodes(cond_path)
            if not sdt_tgt_nodes:
                if self.verbose > 1:
                    print( "[DBG++]: no target nodes found at: %s, returning" % cond_path )
                return False

            tgt_matches = []
            tgt_false_matches = []
            # iterate the properties in the final node of the conditional tree,
            # these are the conditions that we are checking.
            for cond_prop in cond_last_node:
                cond_prop_name = cond_prop.name
                invert_check = ""
                # remove __not__ from the end of a property name, that is an
                # indication for us only, and won't be in the SDT node
                if cond_prop.name.endswith( "__not__" ):
                    cond_prop_name = re.sub( "__not__$", "", cond_prop.name )
                    invert_check = "not"
                if self.verbose > 1:
                    print( "[DBG++]: conditional property:  %s tgt_nodes: %s" % (cond_prop_name,sdt_tgt_nodes) )

                for tgt_node in sdt_tgt_nodes:
                    # is the property present in the target node ?
                    try:
                        tgt_node_prop = tgt_node[cond_prop_name]
                    except:
                        tgt_node_prop = None

                    # no need to compare if the target node doesn't have the property
                    if tgt_node_prop:
                        check_val = cond_prop.compare( tgt_node_prop )

                        # if there was an inversion in the name, flip the result
                        check_val_final = eval( "{0} {1}".format(invert_check, check_val ))
                        if self.verbose > 1:
                            print ( "[DBG++]   ({0}:{1}) condition check final value: {2} {3} was {4}".format(tgt_node.abs_path,tgt_node_prop.value[0],invert_check, check_val, check_val_final ))
                        if check_val_final:
                            # if not already in the list, we need to add the target node
                            if not tgt_node in tgt_matches:
                                tgt_matches.append(tgt_node)
                        else:
                            # if subsequent props are not True, then we need to yank out
                            # the node from our match list
                            if tgt_node in tgt_matches:
                                tgt_matches.remove(tgt_node)
                            # and add it to the false matches list
                            if not tgt_node in tgt_false_matches:
                                tgt_false_matches.append(tgt_node)
                    else:
                        # if it doesn't have it, that has to be a false!
                        if self.verbose:
                            print( "[DBG]: system device tree node '%s' does not have property '%s'" %
                                   (tgt_node,cond_prop_name))

                        # if subsequent props are not True, then we need to yank out
                        # the node from our match list
                        if tgt_node in tgt_matches:
                            tgt_matches.remove(tgt_node)
                        # and add it to the false matches list
                        if not tgt_node in tgt_false_matches:
                            tgt_false_matches.append(tgt_node)

            # loop over the true matches, executing their operations, if one of them returns
            # false, we stop the loop
            for tgt_match in tgt_matches:
                try:
                    # we look through all the subnodes of this lopper operation. If any of them
                    # start with "true", it is a nested lop that we will execute
                    for n in this_lop_subnodes:
                        if n.name.startswith( "true" ):
                            if self.verbose > 1:
                                print( "[DBG++]: true subnode found with lop: %s" % (n['compatible'].value[0] ) )
                            try:
                                # run the lop, passing the target node as an option (the lop may
                                # or may not use it)
                                ret = self.exec_lop( n, lops_tree, { 'start_node' : tgt_match.abs_path } )
                            except Exception as e:
                                print( "[WARNING]: true block had an exception: %s" % e )
                                ret = False

                            # no more looping if the called lop return False
                            if ret == False:
                                if self.verbose > 1:
                                    print( "[DBG++]: code block returned false, stop executing true blocks" )
                                break
                except Exception as e:
                    print( "[WARNING]: conditional had exception: %s" % e )

            # just like the target matches, we iterate any failed matches to see
            # if false blocks were defined.
            for tgt_match in tgt_false_matches:
                # no match, is there a false block ?
                try:
                    for n in this_lop_subnodes:
                        if n.name.startswith( "false" ):
                            if self.verbose > 1:
                                print( "[DBG++]: false subnode found with lop: %s" % (n['compatible'].value[0] ) )

                            try:
                                ret = self.exec_lop( n, lops_tree, { 'start_node' : tgt_match.abs_path } )
                            except Exception as e:
                                print( "[WARNING]: false block had an exception: %s" % e )
                                ret = False

                            # if any of the blocks return False, we are done
                            if ret == False:
                                if self.verbose > 1:
                                    print( "[DBG++]: code block returned false, stop executing true blocks" )
                                break
                except Exception as e:
                    print( "[WARNING]: conditional false block had exception: %s" % e )

        if re.search( ".*,lop,code.*$", lop_type ) or re.search( ".*,lop,xlate.*$", lop_type ):
            # execute a block of python code against a specified start_node
            code = lop_node['code'].value[0]

            if not options:
                options = {}

            try:
                options_spec = lop_node['options'].value
            except:
                options_spec = ""

            try:
                tree_name = lop_node['tree'].value[0]
                try:
                    tree = self.subtrees[tree_name]
                except:
                    print( "[ERROR]: tree name provided (%s), but not found" % tree_name )
                    sys.exit(1)
            except:
                tree = self.tree

            if options_spec:
                for o in options_spec:
                    opt_key,opt_val = o.split(":")
                    if opt_key:
                        options[opt_key] = opt_val

            try:
                start_node = options['start_node']
            except:
                # were there selected nodes ? Make them the context, unless overrriden
                # by an explicit start_node property
                if tree.__selected__:
                    start_node = tree.__selected__[0]
                else:
                    start_node = "/"

            try:
                inherit_list = lop_node['inherit'].value[0].replace(" ","").split(",")
            except:
                inherit_list = []

            if self.verbose:
                print ( "[DBG]: code lop found, node context: %s" % start_node )

            if re.search( ".*,lop,xlate.*$", lop_type ):
                inherit_list.append( "lopper_lib" )

                if tree.__selected__:
                    node_list = tree.__selected__
                else:
                    node_list = [ "/" ]

                for n in node_list:
                    ret = tree.exec_cmd( n, code, options, inherit_list )
                    # who knows what the command did, better sync!
                    tree.sync()
            else:
                ret = tree.exec_cmd( start_node, code, options, inherit_list )
                # who knows what the command did, better sync!
                tree.sync()

            return ret

        if re.search( ".*,lop,modify$", lop_type ):
            node_name = lop_node.name
            if self.verbose:
                print( "[INFO]: node %s is a compatible modify lop" % node_name )
            try:
                prop = lop_node["modify"].value[0]
            except:
                prop = ""

            try:
                tree_name = lop_node['tree'].value[0]
                try:
                    tree = self.subtrees[tree_name]
                except:
                    print( "[ERROR]: tree name provided (%s), but not found" % tree_name )
                    sys.exit(1)
            except:
                tree = self.tree

            try:
                nodes_selection = lop_node["nodes"].value[0]
            except:
                nodes_selection = ""
            if prop:
                if self.verbose:
                    print( "[INFO]: modify property found: %s" % prop )

                # format is: "path":"property":"replacement"
                #    - modify to "nothing", is a remove operation
                #    - modify with no property is node operation (rename or remove)
                modify_expr = prop.split(":")
                # combine these into the assigment, once everything has bee tested
                modify_path = modify_expr[0]
                modify_prop = modify_expr[1]
                modify_val = modify_expr[2]
                if self.verbose:
                    print( "[INFO]: modify path: %s" % modify_expr[0] )
                    print( "        modify prop: %s" % modify_expr[1] )
                    print( "        modify repl: %s" % modify_expr[2] )
                    if nodes_selection:
                        print( "        modify regex: %s" % nodes_selection )

                # if modify_expr[0] (the nodes) is empty, we use the selected nodes
                # if they are available
                if not modify_path:
                    if not tree.__selected__:
                        print( "[WARNING]: no nodes supplied to modify, and no nodes are selected" )
                        return False
                    else:
                        nodes = tree.__selected__
                else:
                    try:
                        nodes = tree.subnodes( tree[modify_path] )
                    except Exception as e:
                        if self.verbose > 1:
                            print( "[DBG+] modify lop: node issue: %s" % e )
                        nodes = []

                if modify_prop:
                    # property operation
                    if not modify_val:
                        if self.verbose:
                            print( "[INFO]: property remove operation detected: %s %s" % (modify_path, modify_prop))

                        try:
                            # TODO: make a special case of the property_modify_below
                            tree.sync()

                            for n in nodes:
                                try:
                                    n.delete( modify_prop )
                                except:
                                    if self.verbose:
                                        print( "[WARNING]: property %s not found, and not deleted" % modify_prop )
                                    # no big deal if it doesn't have the property
                                    pass

                            tree.sync()
                        except Exception as e:
                            print( "[WARNING]: unable to remove property %s/%s (%s)" % (modify_path,modify_prop,e))
                            sys.exit(1)
                    else:
                        if self.verbose:
                            print( "[INFO]: property modify operation detected" )

                        # set the tree state to "syncd", so we'll be able to test for changed
                        # state later.
                        tree.sync()

                        # we re-do the nodes fetch here, since there are slight behaviour/return
                        # differences between nodes() (what this has always used), and subnodes()
                        # which is what we do above. We can re-test and reconcile this in the future.
                        if modify_path:
                            nodes = tree.nodes( modify_path )
                        else:
                            nodes = tree.__selected__

                        if not nodes:
                            if self.verbose:
                                print( "[WARNING]: node %s not found,  property %s not modified " % (modify_path,modify_prop))

                        # if the value has a "&", it is a phandle, and we need
                        # to try and look it up.
                        if re.search( '&', modify_val ):
                            node = modify_val.split( '#' )[0]
                            try:
                                node_property =  modify_val.split( '#' )[1]
                            except:
                                node_property = None

                            phandle_node_name = re.sub( '&', '', node )
                            pfnodes = tree.nodes( phandle_node_name )
                            if not pfnodes:
                                pfnodes = tree.lnodes( phandle_node_name )
                                if not pfnodes:
                                    # was it a local phandle (i.e. in the lop tree?)
                                    pfnodes = lops_tree.nodes( phandle_node_name )
                                    if not pfnodes:
                                        pfnodes = lops_tree.lnodes( phandle_node_name )

                            if node_property:
                                # there was a node property, that means we actualy need
                                # to lookup the phandle and find a property within it. That's
                                # the replacement value
                                if pfnodes:
                                    try:
                                        modify_val = pfnodes[0][node_property].value
                                    except:
                                        modify_val = pfnodes[0].phandle
                                else:
                                    modify_val = 0
                            else:
                                if pfnodes:
                                    phandle = pfnodes[0].phandle
                                else:
                                    phandle = 0

                                modify_val = phandle

                        else:
                            modify_val = Lopper.property_convert( modify_val )

                        for n in nodes:
                            if type( modify_val ) == list:
                                n[modify_prop] = modify_val
                            else:
                                n[modify_prop] = [ modify_val ]

                        tree.sync()
                else:
                    if self.verbose > 1:
                        print( "[DBG+]: modify lop, node operation" )

                    # drop the list, since if we are modifying a node, it is just one
                    # target node.
                    try:
                        node = nodes[0]
                    except:
                        node = None

                    if not node:
                        print( "[ERROR]: no nodes found for %s" % modify_path )
                        sys.exit(1)

                    # node operation
                    # in case /<name>/ was passed as the new name, we need to drop them
                    # since they aren't valid in set_name()
                    if modify_val:
                        modify_source_path = Path(node.abs_path)

                        if modify_val.startswith( "/" ):
                            modify_dest_path = Path( modify_val )
                        else:
                            modify_dest_path = Path( "/" + modify_val )

                        if modify_source_path.parent != modify_dest_path.parent:
                            if self.verbose > 1:
                                print( "[DBG++]: [%s] node move: %s -> %s" %
                                       (tree,modify_source_path,modify_dest_path))
                            # deep copy the node
                            new_dst_node = node()
                            new_dst_node.abs_path = modify_val

                            tree + new_dst_node

                            # delete the old node
                            tree.delete( node )

                            tree.sync()

                        if modify_source_path.name != modify_dest_path.name:
                            if self.verbose > 1:
                                print( "[DBG++]: [%s] node rename: %s -> %s" %
                                       (tree,modify_source_path.name,modify_dest_path.name))

                            modify_val = modify_val.replace( '/', '' )
                            try:

                                # is there already a node at the new destination path ?
                                try:
                                    old_node = tree[str(modify_dest_path)]
                                    if old_node:
                                        # we can error, or we'd have to delete the old one, and
                                        # then let the rename happen. But really, you can just
                                        # write a lop that takes care of that before calling such
                                        # a bad rename lop.
                                        if self.verbose > 1:
                                            print( "[NOTE]: node exists at rename target: %s"  % old_node.abs_path )
                                            print( "        Deleting it, to allow rename to continue" )
                                        tree.delete( old_node )
                                except Exception as e:
                                    # no node at the dest
                                    pass


                                # change the name of the node
                                node.name = modify_val
                                tree.sync()

                            except Exception as e:
                                print( "[ERROR]:cannot rename node '%s' to '%s' (%s)" % (node.abs_path, modify_val, e))
                                sys.exit(1)
                    else:
                        # first we see if the node prefix is an exact match
                        node_to_remove = node

                        if not node_to_remove:
                            print( "[WARNING]: Cannot find node %s for delete operation" % node.abs_path )
                            if self.werror:
                                sys.exit(1)
                        else:
                            try:
                                tree.delete( node_to_remove )
                                tree.sync()
                            except:
                                print( "[WARNING]: could not remove node number: %s" % node_to_remove.abs_path )

        # if the lop didn't return, we return false by default
        return False

    def perform_lops(self):
        """Execute all loaded lops

        Iterates and executes all the loaded lopper operations (lops) for the
        System Device tree.

        The lops are processed in priority order (priority specified at the file
        level), and the rules processed in order as they appear in the lop file.

        lopper operations can immediately process the output of the previous
        operation and hence can be stacked to perform complex operations.

        Args:
            None

        Returns:
            Nothing

        """
        # was --target passed on the command line ?
        if self.target_domain:
            self.domain_spec(target_domain)

        # force verbose output if --dryrun was passed
        if self.dryrun:
            self.verbose = 2

        if self.verbose:
            print( "[NOTE]: \'%d\' lopper operation files will be processed" % len(self.lops))

        lops_runqueue = {}
        for pri in range(1,10):
            lops_runqueue[pri] = []

        # iterate the lops, look for priority. If we find those, we'll run then first
        for x in self.lops:
            if x.fdt:
                lops_fdt = x.fdt
                lops_tree = None
            elif x.dtb:
                lops_fdt = Lopper.dt_to_fdt(x.dtb)
                x.dtb = None
                x.fdt = lops_fdt
            elif x.tree:
                lops_fdt = None
                lops_tree = x.tree

            if lops_fdt:
                lops_tree = LopperTree()
                try:
                    dct = Lopper.export( lops_fdt, strict=True )
                except Exception as e:
                    print( "[ERROR]: (%s) %s" % (x.dts,e) )
                    sys.exit(1)
                lops_tree.load( dct )

                x.tree = lops_tree

            if not lops_tree:
                print( "[ERROR]: invalid lop file %s, cannot process" % x )
                sys.exit(1)

            try:
                ln = lops_tree['/']
                lops_file_priority = ln["priority"].value[0]
            except Exception as e:
                lops_file_priority = 5

            lops_runqueue[lops_file_priority].append(x)

        if self.verbose > 2:
            print( "[DBG+]: lops runqueue: %s" % lops_runqueue )

        # iterate over the lops (by lop-file priority)
        for pri in range(1,10):
            for x in lops_runqueue[pri]:
                fdt_tree = x.tree
                lop_test = re.compile('system-device-tree-v1,lop.*')
                lop_cond_test = re.compile('.*,lop,conditional.*$' )
                skip_list = []
                for f in fdt_tree:
                    if not any(lop_test.match(i) for i in f.type):
                        continue

                    # past here, we know the node is a lop variant, we need one
                    # more check. Is the parent conditional ? if so, we don't
                    # excute it directly.
                    if any( lop_cond_test.match(i) for i in f.type):
                        skip_list = f.subnodes()
                        # for historical resons, the current node is in the subnodes
                        # yank it out or we'll be skipped!
                        skip_list.remove( f )

                    try:
                        noexec = f['noexec']
                    except:
                        noexec = False

                    if noexec or f in skip_list:
                        if self.verbose > 1:
                            print( "[DBG+]: noexec or skip set for: %s" % f.abs_path )
                        continue

                    if self.verbose:
                        print( "[INFO]: ------> processing lop: %s" % f.abs_path )

                    self.exec_lop( f, fdt_tree )


class LopperFile:
    """Internal class to contain the details of a lopper file

    Attributes:
       - dts: the dts source file path for a lop
       - dtb: the compiled dtb file path for a lop
       - fdt: the loaded FDT representation of the dtb

    """
    def __init__(self, lop_file):
        self.dts = lop_file
        self.dtb = ""
        self.fdt = ""

def usage():
    prog = os.path.basename(sys.argv[0])
    print('Usage: %s [OPTION] <system device tree> [<output file>]...' % prog)
    print('  -v, --verbose       enable verbose/debug processing (specify more than once for more verbosity)')
    print('  -t, --target        indicate the starting domain for processing (i.e. chosen node or domain label)' )
    print('    , --dryrun        run all processing, but don\'t write any output files' )
    print('  -d, --dump          dump a dtb as dts source' )
    print('  -i, --input         process supplied input device tree description')
    print('  -a, --assist        load specified python assist (for node or output processing)' )
    print('  -A, --assist-paths  colon separated lists of paths to search for assist loading' )
    print('    , --enhanced      when writing output files, do enhanced processing (this includes phandle replacement, comments, etc' )
    print('    . --auto          automatically run any assists passed via -a' )
    print('    , --permissive    do not enforce fully validated properties (phandles, etc)' )
    print('  -o, --output        output file')
    print('  -x. --xlate         run automatic translations on nodes for indicated input types (yaml,dts)' )
    print('  -f, --force         force overwrite output file(s)')
    print('    , --werror        treat warnings as errors' )
    print('  -S, --save-temps    don\'t remove temporary files' )
    print('  -h, --help          display this help and exit')
    print('  -O, --outdir        directory to use for output files')
    print('    , --server        after processing, start a server for ReST API calls')
    print('    , --version       output the version and exit')
    print('')

def main():
    global inputfiles
    global output
    global output_file
    global sdt
    global sdt_file
    global verbose
    global force
    global dump_dtb
    global target_domain
    global dryrun
    global cmdline_assists
    global werror
    global save_temps
    global enhanced_print
    global outdir
    global load_paths
    global module_name
    global module_args
    global debug
    global server
    global auto_run
    global permissive
    global xlate

    debug = False
    sdt = None
    verbose = 0
    output = ""
    inputfiles = []
    force = False
    dump_dtb = False
    dryrun = False
    target_domain = ""
    cmdline_assists = []
    werror = False
    save_temps = False
    enhanced_print = False
    outdir="./"
    load_paths = []
    server = False
    auto_run = False
    permissive = False
    xlate = []
    try:
        opts, args = getopt.getopt(sys.argv[1:], "A:t:dfvdhi:o:a:SO:Dx:",
                                   [ "debug", "assist-paths=", "outdir", "enhanced",
                                     "save-temps", "version", "werror","target=", "dump",
                                     "force","verbose","help","input=","output=","dryrun",
                                     "assist=","server", "auto", "permissive", "xlate=" ] )
    except getopt.GetoptError as err:
        print('%s' % str(err))
        usage()
        sys.exit(2)

    if opts == [] and args == []:
        usage()
        sys.exit(1)

    for o, a in opts:
        if o in ('-v', "--verbose"):
            verbose = verbose + 1
        elif o in ('-d', "--dump"):
            dump_dtb = True
        elif o in ('-f', "--force"):
            force = True
        elif o in ('-h', '--help'):
            usage()
            sys.exit(0)
        elif o in ('-i', '--input'):
            inputfiles.append(a)
        elif o in ('-a', '--assist'):
            cmdline_assists.append(a)
        elif o in ('-A', '--assist-path'):
            load_paths += a.split(":")
        elif o in ('-O', '--outdir'):
            outdir = a
        elif o in ('-D', '--debug'):
            debug = True
        elif o in ('-t', '--target'):
            target_domain = a
        elif o in ('-o', '--output'):
            output = a
        elif o in ('--dryrun'):
            dryrun=True
        elif o in ('--werror'):
            werror=True
        elif o in ('--server'):
            server=True
        elif o in ('-S', '--save-temps' ):
            save_temps=True
        elif o in ('--enhanced' ):
            enhanced_print = True
        elif o in ('--auto' ):
            auto_run = True
        elif o in ('--permissive' ):
            permissive = True
        elif o in ('-x', '--xlate'):
            xlate.append(a)
        elif o in ('--version'):
            print( "%s" % LOPPER_VERSION )
            sys.exit(0)
        else:
            assert False, "unhandled option"

    # any args should be <system device tree> <output file>
    module_name = ""
    module_args= []
    module_args_found = False
    for idx, item in enumerate(args):
        # validate that the system device tree file exists
        if idx == 0:
            sdt = item
            sdt_file = Path(sdt)
            try:
                my_abs_path = sdt_file.resolve()
            except FileNotFoundError:
                # doesn't exist
                print( "Error: system device tree %s does not exist" % sdt )
                sys.exit(1)

        else:
            if item == "--":
                module_args_found = True

            # the last input is the output file. It can't already exist, unless
            # --force was passed
            if not module_args_found:
                if idx == 1:
                    if output:
                        print( "Error: output was already provided via -o\n")
                        usage()
                        sys.exit(1)
                    else:
                        output = item
                        output_file = Path(output)
                        if output_file.exists():
                            if not force:
                                print( "Error: output file %s exists, and -f was not passed" % output )
                                sys.exit(1)
            else:
                # module arguments
                if not item == "--":
                    if not module_name:
                        module_name = item
                        cmdline_assists.append( item )
                    else:
                        module_args.append( item )

    if module_name and verbose:
        print( "[DBG]: module found: %s" % module_name )
        print( "         args: %s" % module_args )

    if not sdt:
        print( "[ERROR]: no system device tree was supplied\n" )
        usage()
        sys.exit(1)

    if outdir != "./":
        op = Path( outdir )
        try:
            op.resolve()
        except:
            print( "[ERROR]: output directory \"%s\" does not exist" % outdir )
            sys.exit(1)

    # check that the input files (passed via -i) exist
    for i in inputfiles:
        inf = Path(i)
        if not inf.exists():
            print( "Error: input file %s does not exist" % i )
            sys.exit(1)

        valid_ifile_types = [ ".dtsi", ".dtb", ".dts", ".yaml" ]
        itype = Lopper.input_file_type(i)
        if not itype in valid_ifile_types:
            print( "[ERROR]: unrecognized input file type passed" )
            sys.exit(1)


    if xlate:
        for x in xlate:
            # *x_lop gets all remaining splits. We don't always have the ":", so
            # we need that flexibility.
            x_type, *x_lop = x.split(":")

            x_files = []
            if x_lop:
                x_files.append( x_lop[0] )
            else:
                # generate the lop name
                extension = Path(x_type).suffix
                extension = re.sub( "\.", "", extension )
                x_lop_gen = "lop-xlate-{}.dts".format(extension)
                x_files.append( x_lop_gen )

        # check that the xlate files exist
        for x in x_files:
            inf = Path(x)
            if not inf.exists():
                x = "lops/" + x
                inf = Path( x )
                if not inf.exists():
                    print( "[ERROR]: input file %s does not exist" % x )
                    sys.exit(1)

            inputfiles.append( x )


if __name__ == "__main__":

    # Main processes the command line, and sets some global variables we
    # use below
    main()

    if dump_dtb:
        Lopper.dtb_dts_export( sdt, verbose )
        sys.exit(0)

    device_tree = LopperSDT( sdt )

    atexit.register(at_exit_cleanup)

    # set some flags before we process the tree.
    device_tree.dryrun = dryrun
    device_tree.verbose = verbose
    device_tree.werror = werror
    device_tree.output_file = output
    device_tree.cleanup_flag = True
    device_tree.save_temps = save_temps
    device_tree.enhanced = enhanced_print
    device_tree.outdir = outdir
    device_tree.target_domain = target_domain
    device_tree.load_paths = load_paths
    device_tree.permissive = permissive

    device_tree.setup( sdt, inputfiles, "", force )
    device_tree.assists_setup( cmdline_assists )

    if auto_run:
        for a in cmdline_assists:
            assist_args = []
            if a == module_name:
                assist_args = module_args

            device_tree.assist_autorun_setup( a, assist_args )

    else:
        # a "module" is an assist passed after -- on the command line call to
        # lopper.
        if module_name:
            # This sets the trigger node of "/", and makes it autorun
            device_tree.assist_autorun_setup( module_name, module_args )

    if debug:
        import cProfile
        cProfile.run( 'device_tree.perform_lops()' )
    else:
        device_tree.perform_lops()

    if not dryrun:
        # write any changes to the FDT, before we do our write
        Lopper.sync( device_tree.FDT, device_tree.tree.export() )
        device_tree.write( enhanced = device_tree.enhanced )
    else:
        print( "[INFO]: --dryrun was passed, output file %s not written" % output )

    if server:
        if verbose:
            print( "[INFO]: starting WSGI server" )
        lopper_rest.sdt = device_tree
        lopper_rest.app.run()  # run our Flask app
        sys.exit(1)

    device_tree.cleanup()
