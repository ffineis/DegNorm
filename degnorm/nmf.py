from scipy.sparse.linalg import svds
from pandas import DataFrame, concat
from degnorm.utils import *
import warnings
import tqdm
import pickle as pkl
from joblib import Parallel, delayed


class GeneNMFOA():

    def __init__(self, degnorm_iter=5, downsample_rate=1, min_high_coverage=50,
                 nmf_iter=100, bins=20, n_jobs=max_cpu()):
        """
        Initialize an NMF-over-approximator object.

        :param degnorm_iter: int maximum number of NMF-OA iterations to run.
        :param downsample_rate: int reciprocal of downsample rate; a "take every" systematic sampling rate. Use to
        down-sample coverage matrices by taking every r-th nucleotide. When downsample_rate == 1 this is equivalent
        to running DegNorm sans any downsampling.
        :param min_high_coverage: int minimum number of "high coverage" base positions for a gene
        to be considered for baseline selection algorithm. Refer to Supplement, section 2, for definition
        of high-enough coverage.
        :param nmf_iter: int number of iterations to run per NMF-OA approximation per gene's coverage matrix.
        :param bins: int number of bins to use during baseline selection step of NMF-OA loop.
        :param n_jobs: int number of cores used for distributing NMF computations over gene coverage matrices.
        """
        self.degnorm_iter = np.abs(int(degnorm_iter))
        self.nmf_iter = np.abs(int(nmf_iter))
        self.n_jobs = np.abs(int(n_jobs))
        self.bins = np.abs(int(bins))
        self.min_high_coverage = np.abs(int(min_high_coverage))
        self.min_bins = np.ceil(self.bins * 0.3)
        self.use_baseline_selection = None
        self.downsample_rate = np.abs(int(downsample_rate))
        self.mem_splits = None
        self.x = None
        self.x_adj = None
        self.p = None
        self.n_genes = None
        self.scale_factors = None
        self.rho = None
        self.Lis = None
        self.estimates = list()
        self.cov_mats = list()
        self.cov_mats_adj = list()
        self.fitted = False
        self.transformed = False

    def rank_one_approx(self, x):
        """
        Decompose a matrix X via truncated SVD into (K)(E^t) = U_{1} \cdot \sigma_{1}V_{1}

        :param x: numpy 2-d array
        :return: 2-tuple (K, E) matrix factorization
        """
        u, s, v = svds(x, k=1)
        # return u[::-1], s*v[::-1]
        return u*s, v

    def get_high_coverage_idx(self, x):
        """
        Find positions of high coverage in a gene's coverage matrix, defined
        as positions where the sample-wise maximum coverage is at least 10%
        the maximum of the entire coverage array.

        :param x: numpy 2-d array: coverage matrix (p x Li)
        :return: numpy 1-d array of integer base positions
        """
        return np.where(x.max(axis=0) > 0.1 * x.max())[0]

    def nmf(self, x, factors=False):
        """
        Run NMF-OA approximation. See "Normalization of generalized transcript degradation
        improves accuracy in RNA-seq analysis" supplement section 1.2.

        :param x: numpy 2-d array
        :param factors: boolean return 2-tuple of K, E matrix factorization? If False,
        return K.dot(E)
        :return: depending on factors, return (K, E) matrices or K.dot(E) over-approximation to x
        """
        K, E = self.rank_one_approx(x)
        est = K.dot(E)
        lmbda = np.zeros(shape=x.shape)
        c = np.sqrt(self.nmf_iter)

        for _ in range(self.nmf_iter):
            res = est - x
            lmbda -= res * (1 / c)
            lmbda[lmbda < 0.] = 0.
            K, E = self.rank_one_approx(est + lmbda)
            est = K.dot(E)

        if factors:
            return np.abs(K), np.abs(E)

        # quality control - ensure an over-approximation.
        est[est < x] = x[est < x]

        return est

    def run_nmf_serial(self, x, factors=False):
        return list(map(lambda z: self.nmf(z, factors), x))

    def par_apply_nmf(self, dat):
        """
        Apply NMF-OA to a list of coverage matrices.

        :param dat: list of numpy reads coverage arrays, shapes are (Li x p)
        :return: list of dict output from self.nmf, one per numpy.array in dat
        """

        # split up list of coverage matrices to feed to workers.
        dat = split_into_chunks(dat, self.mem_splits)
        nmf_ests = Parallel(n_jobs=self.n_jobs
                                 , verbose=0
                                 , backend='threading')(map(delayed(self.run_nmf_serial), dat))

        return [est for est1d in nmf_ests for est in est1d]

    def compute_scale_factors(self, estimates):
        """
        Update read count scaling factors by computing n x p matrix of degradation index scores from
        a set of NMF-OA estimates.

        Compute s_{j} = \frac{sum_{i=1}^{n}\tilde{X}_{ij}}{median_{j}(\sum_{i=1}^{n}\tilde{X}_{ij})}
        vector (p x 1) of read count adjustment factors.

        :param estimates: list of rank-one approximations of coverage curves
        """
        # construct block rho matrix: len(self.degnorm_genes) x p matrix of DI scores
        est_sums = list(map(lambda x: x.sum(axis=1), estimates))
        self.rho = 1 - self.cov_sums / (np.vstack(est_sums) + 1) # + 1 as per Bin's code.

        # quality control.
        self.rho[self.rho < 0] = 0.
        self.rho[self.rho >= 1] = 1. - 1e-5

        # scale read count matrix by DI scores.
        scaled_counts_mat = self.x / (1 - self.rho)
        scaled_counts_sums = scaled_counts_mat.sum(axis=0)
        self.scale_factors = scaled_counts_sums / np.median(scaled_counts_sums)

    def adjust_coverage_curves(self):
        """
        Adjust coverage curves by dividing the coverage values by corresponding adjustment factor, s_{j}
        """
        self.cov_mats_adj = [(F.T / self.scale_factors).T for F in self.cov_mats]

    def adjust_read_counts(self):
        """
        Adjust read counts by scale factors, while being mindful of
        which genes were run through baseline algorithm and which ones were excluded.
        """

        # determine which genes will be adjusted by self.scale_factors, and which will
        # be updated by 1 - average(DI score) per sample.
        # From Supplement section 2: "For [genes with insufficient coverage], we adjust
        # the read count based on the average DI score of the corresponding sample.

        # TODO: find out if high-coverage judgement done on original or adjusted coverage matrices.
        # update the genes that had sufficient coverage, or were successfully downsampled.
        adj_standard_idx = np.where(self.use_baseline_selection)[0]
        if len(adj_standard_idx) > 0:
            self.x_adj[adj_standard_idx, :] = self.x[adj_standard_idx, :] / (1 - self.rho[adj_standard_idx, :])

        # update the genes that had insufficient coverage by 1 - sample-average DI score.
        adj_low_idx = np.where(~self.use_baseline_selection)[0]
        if len(adj_low_idx) > 0:
            avg_di_score = self.rho.mean(axis=0)
            self.x_adj[adj_low_idx, :] = self.x[adj_low_idx, :] / (1 - avg_di_score)

    def shift_bins(self, bins, dropped_bin):
        """
        Shift bins of consecutive numbers after a bin has been dropped.
        Example:

        shift_bins([[0, 1], [2, 3], [4, 5], [6, 7]], dropped_bin=1)

        [[0, 1], [2, 3], [4, 5]]

        :param bins: list of list of int
        :param dropped_bin: int, the bin that has been deleted from bins.
        :return: list of consecutive integer bins, preserving to the fullest extent
        possible the original bins.
        """
        if (dropped_bin == len(bins)) or (len(bins) == 1):
            return bins
        else:
            if dropped_bin == 0:
                delta = bins[0][0]

            else:
                delta = bins[dropped_bin][0] - bins[(dropped_bin - 1)][-1] - 1

            for bin_idx in range(dropped_bin, len(bins)):
                bins[bin_idx] = [idx - delta for idx in bins[bin_idx]]

        return bins

    def baseline_selection(self, F):
        """
        Find "baseline" region for a gene's coverage curves - a region where it is
        suspected that degradation is minimal, so that the coverage envelope function
        may be estimated more accurately.

        This is typically applied once coverage curves have been scaled by 1 / s_{j}.

        :param F: numpy 2-d array, gene coverage curves, a numpy 2-dimensional array
        :return: 2-tuple (numpy 2-d array, estimate of F post baseline-selection algorithm, boolean
        indicator for whether or not baseline selection algorithm exited early).
        """

        # ------------------------------------------------------------------------- #
        # Overhead:
        # 1. Downsample if desired.
        # 2. Find high-coverage regions of the gene's transcript.
        # 3. If not downsampling, determine whether or not there are sufficient
        #   high-coverage nucleotide positions found in 2.
        # 4. Subset coverage curve to (possibly downsampled) high-coverage regions.
        # 5. Bin-up consecutive regions of high-coverage regions.
        # ------------------------------------------------------------------------- #
        bin_me = True
        ran = False

        # Run systematic downsample if desired, prior to filtering out low-coverage regions.
        if self.downsample_rate > 1:
            _, downsample_idx = self.downsample_2d(F, by_row=False)
            bin_me = False

        hi_cov_idx = self.get_high_coverage_idx(F)
        n_hi_cov = len(hi_cov_idx)

        if bin_me:
            if n_hi_cov < self.min_high_coverage:
                return F, ran

        else:
            # intersect downsampled indices with high-coverage indices.
            hi_cov_idx = np.intersect1d(downsample_idx, hi_cov_idx)
            n_hi_cov = len(hi_cov_idx)
            if n_hi_cov <= 1:
                return F, ran

        # select high-coverage positions from (possibly downsampled) coverage matrix.
        hi_cov_idx.sort()
        F_start = np.copy(F)[:, hi_cov_idx]
        F_bin = np.copy(F_start)
        ran = True

        # obtain initial coverage curve estimate.
        K, E = self.nmf(F_bin, factors=True)
        KE_bin = np.abs(K.dot(E))

        # split up consecutive regions of the high-coverage gene.
        # even in downsampling regime, drop batches of sample points on each baseline selection iteration.
        bin_segs = split_into_chunks(list(range(F_bin.shape[1]))
                                     , n=self.bins)
        n_bins = len(bin_segs)

        # compute DI scores. High score ->> high degradation.
        rho_vec = 1 - F_bin.sum(axis=1) / (KE_bin.sum(axis=1) + 1)  # + 1 as per Bin's code.
        rho_vec[rho_vec < 0] = 0.
        rho_vec[rho_vec >= 1] = 1. - 1e-5

        # ------------------------------------------------------------------------- #
        # Heavy lifting: run baseline selection iterations.
        # Run while the degradation index score is still high.
        # or while there are still non-baseline regions left to trim, before
        # too-many bins have been dropped.
        # If max DI score is <= 0.1, then the baseline selection algorithm has converged; if >= min_bins
        # have been dropped, still exit baseline selection, although this is not convergence in a true sense.
        # ------------------------------------------------------------------------- #
        while (n_bins > self.min_bins) and (np.nanmax(rho_vec) > 0.1):
            n_bins = len(bin_segs)

            # compute relative sum of squared errors by bin.
            ss_r = np.ones(n_bins)
            ss_f = np.ones(n_bins)
            for idx in range(n_bins):
                ss_r[idx] = np.linalg.norm((KE_bin - F_bin)[:, bin_segs[idx]])
                ss_f[idx] = np.linalg.norm(F_bin[:, bin_segs[idx]])

            # safely divide binned residual norms by original binned norms.
            with np.errstate(divide='ignore', invalid='ignore'):
                res_norms = np.true_divide(ss_r, ss_f)
                res_norms[~np.isfinite(res_norms)] = 0

            # # if perfect approximation, exit loop.
            # if np.nanmax(res_norms) == 0:
            #     break

            # drop the bin corresponding to the bin with the maximum normalized residual.
            drop_idx = np.nanargmax(res_norms)
            del bin_segs[drop_idx]

            # collapse the remaining bins into an array of indices to keep.
            keep_idx = np.unique(flatten_2d(bin_segs))

            # update binned segments so that all bins are referencing indices in newly shrunken coverage matrices.
            bin_segs = self.shift_bins(bin_segs
                                       , dropped_bin=drop_idx)

            # shrink F matrix to the indices not dropped.
            # estimate coverage curves from said indices.
            F_bin = F_bin[:, keep_idx]
            KE_bin = self.nmf(F_bin)

            # recompute DI scores: closer to 1 ->> more degradation
            rho_vec = 1 - F_bin.sum(axis=1) / (KE_bin.sum(axis=1) + 1)  # + 1 as per Bin's code.

            # quality control.
            rho_vec[rho_vec < 0] = 0.
            rho_vec[rho_vec >= 1] = 1. - 1e-5

        # determine if baseline selection converged: check whether DI scores are still high.
        # if not converged, just use original NMF factorization as K, E factors instead of baseline selected ones.
        if np.nanmax(rho_vec) < 0.1:
            K, E = self.nmf(F_bin, factors=True)

        # quality control: ensure we never divide F by 0.
        K[K < 1.e-5] = np.min(K[K >= 1.e-5])

        # Use refined estimate of K (p x 1 vector) to refine the envelope estimate,
        # return refined estimate of KE^{T}.
        E = np.true_divide(F.T, K.ravel()).max(axis=1).reshape(-1, 1)

        if np.any(np.isnan(E)):
            raise ValueError('E factor matrix contains np.nans. Aborting.')

        # quality control: ensure a final over-approximation.
        est = np.abs(K.dot(E.T))
        est[est < F] = F[est < F]

        # return estimate of F.
        return est, ran

    def run_baseline_selection_serial(self, x):
        return list(map(self.baseline_selection, x))

    def par_apply_baseline_selection(self, dat):
        """
        Apply baseline selection algorithm to all gene coverage curve matrices in parallel.

        :param dat: list of numpy reads coverage arrays, shapes are (Li x p). Ideally
        coverage arrays will be adjusted for sequencing-depth.
        :return: list of numpy reads coverage arrays estimates
        """
        # split up coverage matrices so that no worker gets much more than 100Mb.
        dat = split_into_chunks(dat, self.mem_splits)
        baseline_ests = Parallel(n_jobs=self.n_jobs
                                 , verbose=0
                                 , backend='threading')(map(delayed(self.run_baseline_selection_serial), dat))

        # flatten results.
        baseline_ests = [est for est1d in baseline_ests for est in est1d]

        # update indicators for whether or not baseline selection ran.
        self.use_baseline_selection = np.array([x[1] for x in baseline_ests])

        return [x[0] for x in baseline_ests]

    @staticmethod
    def _systematic_sample(n, take_every):
        """
        Take an honest to goodness systematic sample of n objects using a take_every gap.
        Randomly initialize a starting point < take every.

        :param n: int size of population to sample from
        :param take_every: int, preferably < n
        :return: 1-d numpy array of sampled integers, length ceiling(n / take_every)
        """
        # if downsample rate larger than n, randomly sample a single point.
        if take_every >= n:
            return int(np.random.choice(n))

        start = np.random.choice(take_every)
        sample_idx = np.arange(start, n
                               , step=take_every
                               , dtype=int)
        return sample_idx

    def downsample_2d(self, x, by_row=True):
        """
        Downsample a coverage matrix at evenly spaced base position indices via systematic sampling.

        :param x: 2-d numpy array; a coverage matrix
        :param by_row: bool indicator - downsample by rows or columns?
        :return: 2-d numpy array with self.grid_points columns
        """
        Li = x.shape[0 if by_row else 1]

        # if downsample rate high enough to cover entire matrix,
        # return input matrix and consider everything sampled.
        if self.downsample_rate == 1:
            return x, np.arange(0, Li)

        if self.downsample_rate >= Li:
            raise ValueError('Cannot downsample at a rate < 1 / length(gene)')

        # obtain systematic sample of transcript positions.
        downsample_idx = self._systematic_sample(Li
                                                 , take_every=self.downsample_rate)

        if by_row:
            return x[downsample_idx, :], downsample_idx

        return x[:, downsample_idx], downsample_idx

    def fit(self, cov_dat, reads_dat):
        """
        Initialize estimates for the DegNorm iteration loop.

        :param cov_dat: Dict of {gene: coverage matrix} pairs;
        coverage matrix shapes are (p x Li) (wide matrices). For each coverage matrix,
        row index is sample number, column index is gene's relative base position on chromosome.
        :param reads_dat: n (genes) x p (samples) numpy array of gene read counts
        """
        self.n_genes = len(cov_dat)
        self.genes = list(cov_dat.keys())
        self.p = next(iter(cov_dat.values())).shape[0]
        self.cov_mats = list(cov_dat.values())
        self.x = np.copy(reads_dat)
        self.x_adj = np.copy(self.x)
        self.use_baseline_selection = np.array([True]*self.n_genes)

        # Store array of gene transcript lengths.
        self.Lis = np.array(list(map(lambda x: x.shape[1], self.cov_mats)))

        # ---------------------------------------------------------------------------- #
        # Run data checks:
        # 1. Check that read count matrix has same number of rows as number of coverage arrays.
        # 2. Check that all coverage arrays are 2-d matrices.
        # 3. Check that all coverage arrays are wider than they are tall.
        # 4. Check that downsample rate < length(gene) for all genes, if downsampling.
        # ---------------------------------------------------------------------------- #
        if self.x.shape[0] != self.n_genes:
            raise ValueError('Number of genes in read count matrix not equal to number of coverage matrices!')

        if not all(map(lambda z: z.ndim == 2, self.cov_mats)):
            raise ValueError('Not all coverage matrices are 2-d arrays!')

        if np.sum(self.Lis / self.p < 1) > 0:
            warnings.warn('At least one coverage matrix slotted for DegNorm is longer than it is wide.'
                          'Ensure that coverage matrices are (p x Li).')

        if self.downsample_rate > 1:
            if not np.min(self.Lis) >= self.downsample_rate:
                raise ValueError('downsample_rate is too large; take-every size > at least one gene.')

        # sum coverage per sample, for later.
        self.cov_sums = np.vstack(list(map(lambda x: x.sum(axis=1), self.cov_mats)))

        # determine (integer) number of data splits for parallel workers (100Mb per worker)
        mem_splits = int(np.ceil(np.sum(list(map(lambda x: x.nbytes, self.cov_mats))) / 1e8))
        self.mem_splits = mem_splits if mem_splits > self.n_jobs else self.n_jobs

        self.fitted = True

    def transform(self):
        if not self.fitted:
            raise ValueError('self.fit not yet executed.')

        # initialize NMF-OA estimates of coverage curves.
        # Only use on genes that meet baseline selection criteria.
        self.estimates = self.par_apply_nmf(self.cov_mats)

        # update vector of read count adjustment factors; update rho (DI) matrix.
        self.compute_scale_factors(self.estimates)
        print('Initial reads scale factors -- {0}'.format(', '.join([str(x) for x in self.scale_factors])))

        # scale baseline_cov coverage curves by 1 / (updated adjustment factors).
        self.adjust_coverage_curves()

        # impose 1 / (1 - DI scores) scaling post self.compute_scale_factors.
        self.adjust_read_counts()

        # Instantiate progress bar.
        pbar = tqdm.tqdm(total = self.degnorm_iter
                         , leave=False
                         , desc='NMF-OA iteration progress')

        # Run DegNorm iterations.
        i = 0
        while i < self.degnorm_iter:

            # run NMF-OA + baseline selection; obtain refined estimates of F.
            self.estimates = self.par_apply_baseline_selection(self.cov_mats_adj)

            # update scale factors, adjust coverage curves and read counts.
            self.compute_scale_factors(self.estimates)
            self.adjust_coverage_curves()
            self.adjust_read_counts()

            print('NMFOA iteration {0} -- reads scale factors: {1}'
                  .format(i + 1, ', '.join([str(x) for x in self.scale_factors])))

            i += 1
            pbar.update()

        pbar.close()

        self.transformed = True

    def fit_transform(self, coverage_dat, reads_dat):
        self.fit(coverage_dat, reads_dat)
        self.transform()

    def save_results(self, gene_manifest_df, output_dir='.',
                     sample_ids=None):
        """
        Save GeneNMFOA output to disk:
            - self.estimates: for each gene's estimated coverage matrix, find the chromosome to which the
            gene belongs. Per chromosome, assemble dictionary of {gene: estimated coverage matrix} pairs,
            and save that dictionary in a directory labeled by the chromosome in the output_dir directory,
            in a file "estimated_coverage_matrices_<chromosome name>.pkl"
            - self.rho: save degradation index scores to "degradation_index_scores.csv" using sample_ids
            as the header.
            - self.x_adj: save adjusted read counts to "adjusted_read_counts.csv" using sample_ids as the header.

        :param gene_manifest_df: pandas.DataFrame establishing chromosome-gene map. Must contain,
        at a minimum, the columns `chr` (str, chromosome) and `gene` (str, gene name).
        :param output_dir: str output directory to save GeneNMFOA output.
        :param sample_ids: (optional) list of str names of RNA_seq experiments, to be used
         as headers for adjusted read counts matrix and degradation index score matrix.
        """
        # quality control.
        if not self.transformed:
            raise ValueError('Model not fitted, not transformed -- NMF-OA has not been run.')

        if not os.path.isdir(output_dir):
            raise IOError('Directory {0} not found.'.format(output_dir))

        # ensure that gene_manifest_df has `chr` and `gene` columns to establish chromosome-gene map.
        if not all([col in gene_manifest_df.columns.tolist() for col in ['chr', 'gene']]):
            raise ValueError('gene_manifest_df must have columns `chr` and `gene`.')

        # if RNA-SEQ sample IDs were not provided, name them.
        if sample_ids:
            if len(sample_ids) != self.p:
                raise ValueError('Number of supplied sample IDs does not match number'
                                 'of samples used to fit GeneNMFOA object.')
        else:
            sample_ids = ['sample_{0}'.format(i + 1) for i in range(self.p)]

        gene_intersect = np.intersect1d(gene_manifest_df.gene.unique(), self.genes)
        if len(gene_intersect) < len(self.genes):
            warnings.warn('Gene manifest data does not encompass set of genes sent through DegNorm.')

        # subset gene manifest to genes run through DegNorm.
        if len(gene_intersect) == 0:
            raise ValueError('No genes used in DegNorm were found in gene manifest dataframe!')

        gene_df = gene_manifest_df[gene_manifest_df.gene.isin(gene_intersect)]
        manifest_chroms = gene_df.chr.unique().tolist()

        # nest per-gene coverage curve estimates within chromosomes.
        chrom_gene_dict = {chrom: dict() for chrom in manifest_chroms}
        for gene in gene_intersect:
            chrom = gene_df[gene_df.gene == gene].chr.iloc[0]
            if chrom not in chrom_gene_dict:
                chrom_gene_dict[chrom] = dict()

            gene_idx = np.where(np.array(self.genes) == gene)[0][0]
            chrom_gene_dict[chrom][gene] = self.estimates[gene_idx]

        # Instantiate results-save progress bar.
        pbar = tqdm.tqdm(total=(self.n_genes + 2)
                         , leave=False
                         , desc='GeneNMFOA results save progress')

        # save estimated coverage matrices to genes nested within chromosomes.
        chrom_gene_dfs = list()
        for chrom in manifest_chroms:
            chrom_dir = os.path.join(output_dir, chrom)

            if not os.path.isdir(chrom_dir):
                os.makedirs(chrom_dir)

            with open(os.path.join(chrom_dir, 'estimated_coverage_matrices_{0}.pkl'.format(chrom)), 'wb') as f:
                pkl.dump(chrom_gene_dict[chrom], f)

            # keep track of chromosome-gene mapping.
            chrom_gene_dfs.append(DataFrame({'chr': chrom
                                          , 'gene': list(chrom_gene_dict[chrom].keys())}))

            pbar.update()

        # order chromosome-gene index according to nmfoa gene order.
        chrom_gene_df = concat(chrom_gene_dfs)
        chrom_gene_df.set_index('gene'
                                , inplace=True)
        chrom_gene_df = chrom_gene_df.loc[self.genes]
        chrom_gene_df.reset_index(inplace=True)
        chrom_gene_df = chrom_gene_df[['chr', 'gene']]

        # append chromosome-gene index to DI scores and save.
        rho_df = DataFrame(self.rho
                           , columns=sample_ids)
        rho_df = concat([chrom_gene_df, rho_df]
                        , axis=1)

        rho_df.to_csv(os.path.join(output_dir, 'degradation_index_scores.csv')
                      , index=False)

        pbar.update()

        # append chromosome-gene index to adjusted read counts and save.
        x_adj_df = DataFrame(self.x_adj
                             , columns=sample_ids)
        x_adj_df = concat([chrom_gene_df, x_adj_df]
                          , axis=1)

        x_adj_df.to_csv(os.path.join(output_dir, 'adjusted_read_counts.csv')
                        , index=False)
        pbar.close()


# if __name__ == '__main__':
#     import pandas as pd
#
#     data_path = os.path.join(os.getenv('HOME'), 'nu/jiping_research/exploration')
#     genes_df = pd.read_csv(os.path.join(data_path, 'gene_exon_metadata.csv'))
#     read_count_df = pd.read_csv(os.path.join(data_path, 'read_counts.csv'))
#     X = read_count_df[list(set(read_count_df.columns) - {'chr', 'gene'})].values
#
#     with open(os.path.join(data_path, 'gene_cov_dict.pkl'), 'rb') as f:
#         gene_cov_dict = pkl.load(f)
#
#     min_gene_length = min(list(map(lambda x: x.shape[1], list(gene_cov_dict.values()))))
#
#     print('read counts matrix shape -- {0}'.format(X.shape))
#     print('genes_df shape -- {0}'.format(genes_df.shape))
#     print('number of coverage matrices -- {0}'.format(len(gene_cov_dict.values())))
#     print('minimum gene length: {0}'.format(min_gene_length))
#
#     print('Executing GeneNMFOA.fit_transform')
#     nmfoa = GeneNMFOA(nmf_iter=10
#                       , downsample_rate=1
#                       , n_jobs=1)
#     nmfoa.fit_transform({k: v for (k, v) in list(gene_cov_dict.items())[0:100]}
#                         , reads_dat=X[0:100, :])
#
#     with open(os.path.join(data_path, 'test_output.pkl'), 'wb') as f:
#         pkl.dump(nmfoa, f)
#
#     print('Saving NMF-OA results.')
#     nmfoa.save_results(gene_manifest_df=genes_df
#                        , output_dir=data_path)
#
#     print(nmfoa.rho)
#     print(nmfoa.x_adj)